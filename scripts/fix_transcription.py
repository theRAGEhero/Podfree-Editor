#!/usr/bin/env python3
"""
Copy a transcript verbatim to a new file (or stdout) without altering the text.

Example:
    python fixTranscription.py source.txt -o verbatim.txt
    python fixTranscription.py source.txt --stdout
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple


logger = logging.getLogger("fixTranscription")
DISCLAIMER = "### Automatic Transcription: it could contain errors."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy a transcript verbatim so it can be shared or archived."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Path to the .txt transcript you want to copy verbatim. "
        "If omitted, the only .txt file in the current directory is used.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Destination file for the verbatim transcript. Defaults to <input>_verbatim.txt.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the transcript to standard output instead of writing a file.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Skip light sanitization (newline normalization, hidden character removal).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging describing what the script is doing.",
    )
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.setLevel(level)


def load_transcript(path: str) -> str:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def derive_output_path(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    ext = ext or ".txt"
    return f"{base}_verbatim{ext}"


def find_single_file_with_suffix(directory: str, suffix: str) -> str:
    matches = sorted(
        os.path.join(directory, entry)
        for entry in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, entry))
        and entry.lower().endswith(suffix.lower())
    )
    if not matches:
        raise FileNotFoundError(f"No *{suffix} files found in {directory}.")
    if len(matches) > 1:
        raise ValueError(
            f"Expected a single *{suffix} file in {directory}, found: "
            + ", ".join(os.path.basename(path) for path in matches)
        )
    return matches[0]


def find_markdown_with_heading(directory: str, heading: str) -> str:
    normalized_heading = heading.strip()
    candidates = []

    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if not os.path.isfile(path) or not entry.lower().endswith(".md"):
            continue

        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        if re.search(rf"(?m)^{re.escape(normalized_heading)}\s*$", content):
            candidates.append(path)

    if not candidates:
        raise FileNotFoundError(
            f"No markdown file with heading '{normalized_heading}' found in {directory}."
        )
    if len(candidates) > 1:
        raise ValueError(
            "Multiple markdown files contain the heading "
            f"'{normalized_heading}': "
            + ", ".join(os.path.basename(path) for path in candidates)
        )
    return candidates[0]


def replace_markdown_section(markdown_path: str, heading: str, body: str) -> None:
    with open(markdown_path, "r", encoding="utf-8") as fh:
        original = fh.read()

    normalized_heading = heading.strip()
    heading_match = re.search(rf"(?m)^{re.escape(normalized_heading)}\s*$", original)
    if not heading_match:
        raise ValueError(f"Section '{normalized_heading}' not found in {markdown_path}.")

    heading_level = len(normalized_heading) - len(normalized_heading.lstrip("#"))
    section_start = heading_match.end()

    boundary_pattern = re.compile(rf"(?m)^#{{1,{heading_level}}} ")
    boundary_match = boundary_pattern.search(original, section_start)
    section_end = boundary_match.start() if boundary_match else len(original)

    cleaned_body = body.strip("\n")
    if cleaned_body:
        cleaned_body += "\n"

    replacement = f"{normalized_heading}\n\n{cleaned_body}"

    trailing = original[section_end:]
    if trailing and not trailing.startswith("\n"):
        replacement += "\n"

    updated = original[: heading_match.start()] + replacement + trailing.lstrip("\n")
    if not updated.endswith("\n"):
        updated += "\n"

    with open(markdown_path, "w", encoding="utf-8") as fh:
        fh.write(updated)


@dataclass
class SanitizeStats:
    normalized_newlines: int = 0
    nbsp_replaced: int = 0
    swung_dash_replaced: int = 0
    zero_width_removed: int = 0
    control_chars_removed: int = 0
    trailing_whitespace_trimmed: int = 0
    blank_lines_collapsed: int = 0
    ellipses_removed: int = 0
    added_terminal_newline: bool = False

    @property
    def changed(self) -> bool:
        return any(
            [
                self.normalized_newlines,
                self.nbsp_replaced,
                self.swung_dash_replaced,
                self.zero_width_removed,
                self.control_chars_removed,
                self.trailing_whitespace_trimmed,
                self.blank_lines_collapsed,
                self.ellipses_removed,
                self.added_terminal_newline,
            ]
        )


ZERO_WIDTH_CHARS = (
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\ufeff",  # zero width no-break space / BOM
)
SWUNG_DASH = "\u2053"
# Treat these characters as sentence terminators when evaluating paragraph breaks.
SENTENCE_ENDING_CHARS = ".?!;:"


def sanitize_transcript(text: str) -> tuple[str, SanitizeStats]:
    stats = SanitizeStats()

    if "\r" in text:
        stats.normalized_newlines = text.count("\r")
        text = text.replace("\r\n", "\n").replace("\r", "\n")

    if "\u00a0" in text:
        stats.nbsp_replaced = text.count("\u00a0")
        text = text.replace("\u00a0", " ")

    if SWUNG_DASH in text:
        stats.swung_dash_replaced = text.count(SWUNG_DASH)
        text = text.replace(SWUNG_DASH, "...")

    zero_width_total = 0
    for ch in ZERO_WIDTH_CHARS:
        if ch in text:
            count = text.count(ch)
            zero_width_total += count
            text = text.replace(ch, "")
    stats.zero_width_removed = zero_width_total

    cleaned_chars: list[str] = []
    for ch in text:
        if ch in ("\n", "\t"):
            cleaned_chars.append(ch)
            continue
        if unicodedata.category(ch).startswith("C"):
            stats.control_chars_removed += 1
            continue
        cleaned_chars.append(ch)
    text = "".join(cleaned_chars)

    text, ellipses_removed = re.subn(r"\s*\.{3,}\s*", " ", text)
    stats.ellipses_removed = ellipses_removed

    text, stats.trailing_whitespace_trimmed = re.subn(r"[ \t]+\n", "\n", text)
    text, stats.blank_lines_collapsed = re.subn(r"\n{3,}", "\n\n", text)

    if text and not text.endswith("\n"):
        text += "\n"
        stats.added_terminal_newline = True

    return text, stats


def log_sanitization_stats(stats: SanitizeStats) -> None:
    if not stats.changed:
        logger.info("Sanitization: transcript already clean; no adjustments made.")
        return

    logger.info(
        "Sanitization applied: normalized_newlines=%d, nbsp_replaced=%d, swung_dash_replaced=%d, "
        "zero_width_removed=%d, control_chars_removed=%d, trailing_whitespace_trimmed=%d, "
        "blank_lines_collapsed=%d, ellipses_removed=%d, added_terminal_newline=%s",
        stats.normalized_newlines,
        stats.nbsp_replaced,
        stats.swung_dash_replaced,
        stats.zero_width_removed,
        stats.control_chars_removed,
        stats.trailing_whitespace_trimmed,
        stats.blank_lines_collapsed,
        stats.ellipses_removed,
        "yes" if stats.added_terminal_newline else "no",
    )


def format_transcript(text: str) -> str:
    """Reflow transcript blocks while preserving speaker labels and wording."""
    stripped = text.strip("\n")
    if not stripped:
        return ""

    lines = stripped.split("\n")
    blocks: List[Tuple[Optional[str], List[str]]] = []
    current_speaker: Optional[str] = None
    current_lines: List[str] = []

    for raw_line in lines:
        line = raw_line.rstrip()
        if is_speaker_line(line):
            if current_speaker is not None or any(l.strip() for l in current_lines):
                blocks.append((current_speaker, current_lines))
            current_speaker = line.strip()
            current_lines = []
            continue

        current_lines.append(line)

    if current_speaker is not None or any(l.strip() for l in current_lines):
        blocks.append((current_speaker, current_lines))

    formatted_blocks: List[str] = []
    for speaker, content_lines in blocks:
        formatted_content = format_content_lines(content_lines)
        if speaker:
            block_parts = [speaker]
            if formatted_content:
                block_parts.append(formatted_content)
            formatted_blocks.append("\n".join(block_parts))
        else:
            if formatted_content:
                formatted_blocks.append(formatted_content)

    if not formatted_blocks:
        return text if text.endswith("\n") else text + "\n"

    return "\n\n".join(formatted_blocks) + ("\n" if not formatted_blocks[-1].endswith("\n") else "")


SPEAKER_REGEX = re.compile(r"^[^(\n]+ \(\d{2}:\d{2}\)")


def is_speaker_line(line: str) -> bool:
    return bool(SPEAKER_REGEX.match(line.strip()))


def format_content_lines(lines: List[str]) -> str:
    trimmed = trim_blank_edges(lines)
    if not trimmed:
        return ""

    paragraphs: List[str] = []
    current_chunk: List[str] = []
    pending_blank = False
    forced_break = False

    for line in trimmed:
        stripped = line.strip()
        if not stripped:
            if pending_blank:
                forced_break = True
            else:
                pending_blank = True
            continue

        if pending_blank:
            prev_text = current_chunk[-1] if current_chunk else None
            if forced_break or should_break_between(prev_text, stripped):
                if current_chunk:
                    paragraphs.append(join_words(current_chunk))
                current_chunk = [stripped]
            else:
                current_chunk.append(stripped)
            pending_blank = False
            forced_break = False
            continue

        current_chunk.append(stripped)

    if current_chunk:
        paragraphs.append(join_words(current_chunk))

    return "\n\n".join(paragraphs)


def trim_blank_edges(lines: List[str]) -> List[str]:
    if not lines:
        return []
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def should_break_between(prev_text: Optional[str], next_text: str) -> bool:
    if prev_text is None:
        return False

    prev_sentence_end = last_sentence_char(prev_text)
    next_initial = first_alpha_char(next_text)

    if prev_sentence_end is None:
        return False

    if prev_sentence_end in SENTENCE_ENDING_CHARS and next_initial and next_initial.isupper():
        if len(prev_text.split()) <= 3:
            return False
        return True

    return False


def last_sentence_char(text: str) -> Optional[str]:
    idx = len(text) - 1
    while idx >= 0 and text[idx] in "\"'”)]":
        idx -= 1
    if idx < 0:
        return None
    return text[idx]


def first_alpha_char(text: str) -> Optional[str]:
    for ch in text:
        if ch.isalpha():
            return ch
    return None


def join_words(words: List[str]) -> str:
    return " ".join(words)


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    if args.stdout and args.output:
        logger.error("Cannot use --stdout and --output together.")
        sys.stderr.write("Choose either --stdout or --output, not both.\n")
        sys.exit(1)

    if args.input_file:
        input_path = args.input_file
    else:
        try:
            input_path = find_single_file_with_suffix(os.getcwd(), ".txt")
            logger.info("Auto-detected transcript: %s", os.path.basename(input_path))
        except (FileNotFoundError, ValueError) as exc:
            logger.error("%s", exc)
            sys.stderr.write(f"{exc}\n")
            sys.exit(1)

    try:
        logger.info("Reading transcript from %s", input_path)
        transcript = load_transcript(input_path)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)
    except OSError as exc:
        logger.exception("Failed to read transcript.")
        sys.stderr.write(f"Failed to read input file: {exc}\n")
        sys.exit(1)

    if not transcript.strip():
        logger.error("The input transcript is empty.")
        sys.stderr.write("Input transcript is empty; nothing to copy.\n")
        sys.exit(1)

    if args.no_clean:
        logger.info("Skipping sanitization (--no-clean supplied).")
        cleaned = transcript
        stats = None
    else:
        cleaned, stats = sanitize_transcript(transcript)
        log_sanitization_stats(stats)

    formatted = format_transcript(cleaned)

    if formatted.strip():
        formatted_with_disclaimer = (
            f"{DISCLAIMER}\n\n{formatted.strip()}\n"
        )
    else:
        formatted_with_disclaimer = f"{DISCLAIMER}\n"

    if args.stdout:
        logger.info("Writing transcript to stdout.")
        sys.stdout.write(formatted_with_disclaimer)
        if not formatted_with_disclaimer.endswith("\n"):
            sys.stdout.write("\n")
        return

    if args.output:
        output_path = args.output
        logger.info("Writing verbatim transcript to %s", output_path)
        try:
            with open(output_path, "w", encoding="utf-8") as fh:
                fh.write(
                    formatted_with_disclaimer
                    if formatted_with_disclaimer.endswith("\n")
                    else formatted_with_disclaimer + "\n"
                )
        except OSError as exc:
            logger.exception("Failed to write transcript.")
            sys.stderr.write(f"Failed to write output: {exc}\n")
            sys.exit(1)
        print(f"Verbatim transcript written to: {output_path}")
        return

    try:
        markdown_path = find_markdown_with_heading(os.getcwd(), "## Blog Post")
        logger.info(
            "Updating section '## Blog Post' in %s", os.path.basename(markdown_path)
        )
        replace_markdown_section(
            markdown_path, "## Blog Post", formatted_with_disclaimer
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)

    print(f"Updated '## Blog Post' section in: {markdown_path}")


if __name__ == "__main__":
    main()
