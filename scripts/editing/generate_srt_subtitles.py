#!/usr/bin/env python3
"""
Generate SRT subtitles from JSON transcription file.

Reads the transcription JSON (created by deepgram_transcribe_debates.py) and
generates an SRT subtitle file with speaker labels and timestamps.
"""

import json
import re
import sys
from pathlib import Path


def format_srt_timestamp(seconds: float) -> str:
    """
    Convert seconds to SRT timestamp format: HH:MM:SS,mmm

    Args:
        seconds: Time in seconds (can be float)

    Returns:
        Formatted timestamp string (e.g., "00:01:23,456")
    """
    # Ensure non-negative
    seconds = max(0, seconds)

    # Split into whole seconds and milliseconds
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)

    # Calculate hours, minutes, seconds
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def extract_speaker_name(speaker_id: str) -> str:
    """
    Extract friendly speaker name from identifier.

    Args:
        speaker_id: Speaker identifier (e.g., "speaker_0")

    Returns:
        Friendly name (e.g., "Speaker 0")
    """
    if speaker_id.startswith("speaker_"):
        number = speaker_id.split("_", 1)[-1]
        return f"Speaker {number}"
    return speaker_id


def split_text_into_chunks(text: str, max_chars: int = 84) -> list[str]:
    """
    Split text into subtitle-sized chunks with smart boundaries.

    Splits at sentence boundaries first, then commas, then word boundaries.
    Ensures each chunk is readable and under the character limit.

    Args:
        text: Text to split
        max_chars: Maximum characters per chunk (default 84 for 2-line subtitles)

    Returns:
        List of text chunks
    """
    # If text already fits, return as-is
    if len(text) <= max_chars:
        return [text]

    chunks = []

    # Split at sentence boundaries first (. ! ?)
    sentences = re.split(r'([.!?]+\s*)', text)
    # Rejoin sentence with its punctuation
    sentences = [''.join(sentences[i:i+2]).strip() for i in range(0, len(sentences)-1, 2)]
    if len(sentences) % 2 != 0:  # Handle last item if odd
        sentences.append(sentences[-1])

    # If no sentences were found (no punctuation), treat entire text as one sentence
    if not sentences:
        sentences = [text]

    current_chunk = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # If sentence alone exceeds limit, split at commas
        if len(sentence) > max_chars:
            # Try splitting at commas
            parts = re.split(r'(,\s*)', sentence)
            parts = [''.join(parts[i:i+2]).strip() for i in range(0, len(parts)-1, 2)]
            if len(parts) % 2 != 0:
                parts.append(parts[-1])

            for part in parts:
                part = part.strip()
                if not part:
                    continue

                # If part still too long, split at words
                if len(part) > max_chars:
                    words = part.split()
                    word_chunk = ""

                    for word in words:
                        test_chunk = word_chunk + (" " if word_chunk else "") + word

                        if len(test_chunk) <= max_chars:
                            word_chunk = test_chunk
                        else:
                            if word_chunk:
                                chunks.append(word_chunk.strip())
                            word_chunk = word

                    if word_chunk:
                        if current_chunk and len(current_chunk) + len(word_chunk) + 1 <= max_chars:
                            current_chunk += " " + word_chunk
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = word_chunk

                # Part fits, try adding to current chunk
                elif current_chunk and len(current_chunk) + len(part) + 1 <= max_chars:
                    current_chunk += " " + part

                # Start new chunk
                elif current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = part
                else:
                    current_chunk = part

        # Sentence fits, try adding to current chunk
        elif current_chunk and len(current_chunk) + len(sentence) + 1 <= max_chars:
            current_chunk += " " + sentence

        # Start new chunk
        elif current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            current_chunk = sentence

    # Add final chunk
    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text]


def distribute_timing(start: float, end: float, chunks: list[str]) -> list[tuple[float, float]]:
    """
    Distribute timing proportionally across text chunks based on character count.

    Args:
        start: Start time in seconds
        end: End time in seconds
        chunks: List of text chunks

    Returns:
        List of (start_time, end_time) tuples for each chunk
    """
    if not chunks:
        return []

    if len(chunks) == 1:
        return [(start, end)]

    total_duration = end - start
    total_chars = sum(len(chunk) for chunk in chunks)

    # Minimum duration per subtitle (0.8 seconds)
    min_duration = 0.8

    timings = []
    current_start = start

    for i, chunk in enumerate(chunks):
        # Calculate proportional duration based on character count
        chunk_chars = len(chunk)
        chunk_duration = total_duration * (chunk_chars / total_chars)

        # Enforce minimum duration
        chunk_duration = max(min_duration, chunk_duration)

        chunk_end = current_start + chunk_duration

        # For last chunk, use exact end time to avoid drift
        if i == len(chunks) - 1:
            chunk_end = end

        timings.append((current_start, chunk_end))
        current_start = chunk_end

    return timings


def generate_srt_from_json(json_path: Path, output_path: Path) -> None:
    """
    Generate SRT subtitle file from transcription JSON.

    Args:
        json_path: Path to the transcription JSON file
        output_path: Path where SRT file will be saved
    """
    # Read JSON file
    print(f"📖 Reading transcription from: {json_path}")
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Extract contributions
    contributions = data.get('contributions', [])

    if not contributions:
        print("❌ Error: No contributions found in JSON file")
        sys.exit(1)

    print(f"📝 Found {len(contributions)} contributions")

    # Generate SRT content
    srt_lines = []
    subtitle_number = 1

    for contrib in contributions:
        # Extract timing and text
        start_time = contrib.get('start_time_seconds', 0)
        end_time = contrib.get('end_time_seconds', 0)
        text = contrib.get('text', '').strip()

        # Skip empty contributions
        if not text:
            continue

        # Split text into readable chunks (max 84 chars per subtitle)
        chunks = split_text_into_chunks(text, max_chars=84)

        # Distribute timing proportionally across chunks
        timings = distribute_timing(start_time, end_time, chunks)

        # Create one SRT entry per chunk
        for chunk, (chunk_start, chunk_end) in zip(chunks, timings):
            # Format timestamps
            start_timestamp = format_srt_timestamp(chunk_start)
            end_timestamp = format_srt_timestamp(chunk_end)

            # Build SRT entry (no speaker labels)
            # Format:
            # 1
            # 00:00:00,000 --> 00:00:05,000
            # Subtitle text
            # (blank line)
            srt_lines.append(f"{subtitle_number}")
            srt_lines.append(f"{start_timestamp} --> {end_timestamp}")
            srt_lines.append(chunk)
            srt_lines.append("")  # Blank line between entries

            subtitle_number += 1

    # Write SRT file
    print(f"💾 Writing SRT file to: {output_path}")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(srt_lines))

    total_subtitles = subtitle_number - 1
    print(f"✅ Successfully generated SRT with {total_subtitles} subtitles")
    print(f"   Output: {output_path}")


def main():
    """Main entry point - find JSON and generate SRT."""
    # Get current working directory (project directory)
    workdir = Path.cwd()

    print(f"🔍 Searching for transcription JSON in: {workdir}")

    # Look for transcription JSON file with multiple patterns
    # Priority 1: Deliberation JSON in subdirectory (preferred format)
    deliberation_dir = workdir / "Deliberation Json"
    if deliberation_dir.is_dir():
        transcript_files = list(deliberation_dir.glob("*_deliberation.json"))
        if transcript_files:
            print(f"   Found deliberation JSON in subdirectory")
    else:
        transcript_files = []

    # Priority 2: Raw JSON in main directory
    if not transcript_files:
        transcript_files = list(workdir.glob("*_raw.json"))
        if transcript_files:
            print(f"   Found raw transcription JSON")

    # Priority 3: Legacy patterns
    if not transcript_files:
        transcript_files = list(workdir.glob("*_transcript.json"))

    if not transcript_files:
        transcript_files = list(workdir.glob("*transcript*.json"))

    if not transcript_files:
        print("❌ Error: No transcription JSON file found in current directory")
        print("   Expected patterns:")
        print("   - Deliberation Json/*_deliberation.json")
        print("   - *_raw.json")
        print("   - *_transcript.json or *transcript*.json")
        sys.exit(1)

    # Use the first (or only) transcript file found
    json_path = transcript_files[0]

    if len(transcript_files) > 1:
        print(f"⚠️  Multiple transcription files found, using: {json_path.name}")

    # Generate output filename: replace .json with .srt
    # Always save in the main project directory (not in subdirectory)
    srt_filename = json_path.stem + '.srt'  # e.g., "bruce_final_deliberation.srt"
    output_path = workdir / srt_filename

    # Check if SRT already exists
    if output_path.exists():
        print(f"⚠️  Warning: SRT file already exists and will be overwritten")
        print(f"   Existing file: {output_path}")

    # Generate SRT (no speaker labels, with text chunking)
    generate_srt_from_json(json_path, output_path)


if __name__ == "__main__":
    main()
