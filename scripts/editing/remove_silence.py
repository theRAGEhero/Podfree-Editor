#!/usr/bin/env python3
"""Remove silence from audio files using previously detected silence tags.

This script reads silence intervals from a JSON file created by detect_silence.py
and uses ffmpeg to remove those sections, creating a cleaned audio file.

Usage:
    python remove_silence.py [silence_tags_json] [--output cleaned_audio.mp3]

Arguments:
    silence_tags_json: Path to the silence tags JSON file. Auto-detects if omitted.
    --output: Output file path (default: <original>_no_silence.mp3)

The script uses ffmpeg's select and aselect filters to keep only non-silent sections
and concatenates them into a single output file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def find_silence_tags_file() -> Path:
    """Auto-detect the silence tags JSON file in the workspace."""
    candidates = [
        p for p in Path.cwd().iterdir()
        if p.suffix == ".json" and "_silence" in p.stem and p.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            "No silence tags file (*_silence.json) found. Run detect_silence.py first."
        )

    if len(candidates) == 1:
        return candidates[0]

    # Use the most recent file
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        raise ValueError(
            "Multiple silence tag files found. Specify the input JSON explicitly."
        ) from None


def load_silence_tags(tags_path: Path) -> tuple[str, list[dict[str, float]], dict]:
    """Load silence intervals from the JSON file."""
    with tags_path.open("r") as f:
        data = json.load(f)

    source_file = data.get("metadata", {}).get("source_file")
    if not source_file:
        raise ValueError("Invalid silence tags file: missing source_file in metadata")

    intervals = data.get("silence_intervals", [])
    metadata = data.get("metadata", {})

    return source_file, intervals, metadata


def build_keep_intervals(
    silence_intervals: list[dict[str, float]], total_duration: float
) -> list[tuple[float, float]]:
    """
    Convert silence intervals to keep intervals (the non-silent parts).

    Returns a list of (start, end) tuples for segments to keep.
    """
    if not silence_intervals:
        return [(0.0, total_duration)]

    keep_intervals = []
    current_pos = 0.0

    for silence in silence_intervals:
        silence_start = silence["start"]
        silence_end = silence["end"]

        # Add the segment before this silence
        if silence_start > current_pos:
            keep_intervals.append((current_pos, silence_start))

        current_pos = silence_end

    # Add the final segment after the last silence
    if current_pos < total_duration:
        keep_intervals.append((current_pos, total_duration))

    return keep_intervals


def get_audio_duration(audio_path: Path) -> float:
    """Get the total duration of an audio file in seconds."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(f"Failed to get audio duration: {exc}") from exc


def remove_silence_ffmpeg(
    input_path: Path, output_path: Path, keep_intervals: list[tuple[float, float]]
) -> None:
    """
    Use ffmpeg to extract and concatenate non-silent segments.

    This creates temporary files for each segment and then concatenates them.
    """
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Create a temporary directory for segments
    temp_dir = output_path.parent / ".temp_segments"
    temp_dir.mkdir(exist_ok=True)

    try:
        segment_files = []

        # Extract each keep interval as a separate file
        for i, (start, end) in enumerate(keep_intervals):
            segment_file = temp_dir / f"segment_{i:04d}.mp3"
            duration = end - start

            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(input_path),
                "-ss", str(start),
                "-t", str(duration),
                "-c:a", "libmp3lame",
                "-b:a", "320k",
                "-ar", "48000",
                str(segment_file),
            ]

            subprocess.run(cmd, capture_output=True, check=True)
            segment_files.append(segment_file)
            print(f"  Extracted segment {i + 1}/{len(keep_intervals)}: {start:.2f}s - {end:.2f}s")

        # Create a concat demuxer file list
        concat_file = temp_dir / "concat_list.txt"
        with concat_file.open("w") as f:
            for segment in segment_files:
                f.write(f"file '{segment.name}'\n")

        # Concatenate all segments
        print(f"  Concatenating {len(segment_files)} segments...")
        concat_cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output_path),
        ]

        subprocess.run(concat_cmd, capture_output=True, check=True)

    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not installed or not in PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}") from exc
    finally:
        # Clean up temporary files
        if temp_dir.exists():
            for temp_file in temp_dir.iterdir():
                temp_file.unlink()
            temp_dir.rmdir()


def build_output_path(input_path: Path, explicit: Optional[str]) -> Path:
    """Build the output file path."""
    if explicit:
        output = Path(explicit).expanduser()
        if output.is_dir():
            return output / (input_path.stem + "_no_silence.mp3")
        return output

    return input_path.with_stem(input_path.stem + "_no_silence")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove silence from audio using detected silence tags."
    )
    parser.add_argument("input", nargs="?", help="Path to the silence tags JSON file")
    parser.add_argument("--output", help="Destination audio file (optional)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        # Find and load silence tags
        tags_path = Path(args.input).expanduser() if args.input else find_silence_tags_file()
        if not tags_path.is_file():
            raise FileNotFoundError(f"Silence tags file not found: {tags_path}")

        print(f"Loading silence tags from {tags_path.name}...")
        source_file, silence_intervals, metadata = load_silence_tags(tags_path)

        # Find the source audio file
        source_path = Path.cwd() / source_file
        if not source_path.is_file():
            raise FileNotFoundError(f"Source audio file not found: {source_file}")

        # Get audio duration
        total_duration = get_audio_duration(source_path)
        print(f"Source: {source_file} ({total_duration:.1f}s)")

        # Build keep intervals
        keep_intervals = build_keep_intervals(silence_intervals, total_duration)

        if not keep_intervals:
            print("No audio segments to keep (entire file is silent?).")
            return 1

        # Calculate stats
        keep_duration = sum(end - start for start, end in keep_intervals)
        removed_duration = total_duration - keep_duration
        removed_percentage = (removed_duration / total_duration) * 100 if total_duration > 0 else 0

        print(f"Removing {len(silence_intervals)} silent sections ({removed_duration:.1f}s, {removed_percentage:.1f}%)")
        print(f"Keeping {len(keep_intervals)} segments ({keep_duration:.1f}s)")
        print()

        # Build output path
        output_path = build_output_path(source_path, args.output)

        # Remove silence using ffmpeg
        print("Processing with ffmpeg...")
        remove_silence_ffmpeg(source_path, output_path, keep_intervals)

        # Print summary
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print()
        print(f"✅ Silence removed successfully!")
        print(f"   Output: {output_path.name} ({size_mb:.1f} MB)")
        print(f"   Duration: {keep_duration:.1f}s (saved {removed_duration:.1f}s)")

        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
