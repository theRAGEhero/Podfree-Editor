#!/usr/bin/env python3
"""Detect silence in audio files and tag them for later removal.

This script analyzes an audio file to find silent sections based on configurable
thresholds and saves the detected silence intervals to a JSON file that can be
used by the remove_silence.py script.

Usage:
    python detect_silence.py [input_audio] [--noise-threshold -50dB] [--min-duration 0.5]

Arguments:
    input_audio: Path to the audio file (MP3, WAV, FLAC). Auto-detects if omitted.
    --noise-threshold: Volume threshold in dB (default: -50dB). Lower = stricter silence.
    --min-duration: Minimum silence duration in seconds (default: 0.5s).

Output:
    Creates a JSON file with silence intervals: <audio_basename>_silence.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SUPPORTED_AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg")


def find_audio_file() -> Path:
    """Auto-detect the main audio file in the workspace."""
    candidates = [
        p for p in Path.cwd().iterdir() if p.suffix.lower() in SUPPORTED_AUDIO_EXTS and p.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No audio files ({', '.join(SUPPORTED_AUDIO_EXTS)}) found in current directory."
        )

    if len(candidates) == 1:
        return candidates[0]

    # Prefer the largest file when multiple exist
    try:
        return max(candidates, key=lambda path: path.stat().st_size)
    except OSError:
        raise ValueError(
            "Multiple audio files found. Specify the input audio explicitly."
        ) from None


def detect_silence(
    input_path: Path, noise_threshold: str, min_duration: float
) -> list[dict[str, float]]:
    """
    Use ffmpeg's silencedetect filter to find silent sections.

    Returns a list of silence intervals with start and end times in seconds.
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-af",
        f"silencedetect=noise={noise_threshold}:d={min_duration}",
        "-f",
        "null",
        "-",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not installed or not in PATH") from exc
    except subprocess.CalledProcessError as exc:
        # ffmpeg writes to stderr even on success for filters
        result = exc

    # Parse ffmpeg stderr output for silence detection
    # Example lines:
    # [silencedetect @ 0x...] silence_start: 12.5
    # [silencedetect @ 0x...] silence_end: 15.2 | silence_duration: 2.7
    output = result.stderr if hasattr(result, 'stderr') else ""

    silence_starts = re.findall(r"silence_start: ([\d.]+)", output)
    silence_ends = re.findall(r"silence_end: ([\d.]+)", output)

    # Build intervals (start, end)
    intervals = []
    for i in range(min(len(silence_starts), len(silence_ends))):
        start = float(silence_starts[i])
        end = float(silence_ends[i])
        duration = end - start
        intervals.append({
            "start": start,
            "end": end,
            "duration": duration
        })

    # Handle case where silence extends to the end of the file
    if len(silence_starts) > len(silence_ends):
        # Get audio duration
        duration_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        try:
            duration_result = subprocess.run(
                duration_cmd, capture_output=True, text=True, check=True
            )
            total_duration = float(duration_result.stdout.strip())
            start = float(silence_starts[-1])
            intervals.append({
                "start": start,
                "end": total_duration,
                "duration": total_duration - start
            })
        except (subprocess.CalledProcessError, ValueError):
            pass  # Skip if we can't get duration

    return intervals


def save_silence_tags(output_path: Path, intervals: list[dict[str, float]], metadata: dict) -> None:
    """Save detected silence intervals to a JSON file."""
    data = {
        "metadata": metadata,
        "silence_intervals": intervals,
        "total_silence_count": len(intervals),
        "total_silence_duration": sum(i["duration"] for i in intervals)
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(data, f, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect silence in audio files and save tags for removal."
    )
    parser.add_argument("input", nargs="?", help="Path to the audio file")
    parser.add_argument(
        "--noise-threshold",
        default="-50dB",
        help="Volume threshold in dB (default: -50dB). Lower values = stricter silence detection."
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=0.5,
        help="Minimum silence duration in seconds (default: 0.5)"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        # Find input audio file
        input_path = Path(args.input).expanduser() if args.input else find_audio_file()
        if not input_path.is_file():
            raise FileNotFoundError(f"Audio file not found: {input_path}")

        print(f"Analyzing {input_path.name} for silence...")
        print(f"  Threshold: {args.noise_threshold}")
        print(f"  Min Duration: {args.min_duration}s")
        print()

        # Detect silence
        intervals = detect_silence(input_path, args.noise_threshold, args.min_duration)

        if not intervals:
            print("No silence detected with current thresholds.")
            return 0

        # Save to JSON
        output_path = input_path.with_stem(input_path.stem + "_silence").with_suffix(".json")
        metadata = {
            "source_file": input_path.name,
            "noise_threshold": args.noise_threshold,
            "min_duration": args.min_duration
        }
        save_silence_tags(output_path, intervals, metadata)

        # Print summary
        total_silence = sum(i["duration"] for i in intervals)
        print(f"✅ Found {len(intervals)} silent sections ({total_silence:.1f}s total)")
        print(f"   Tags saved to: {output_path.name}")
        print()
        print("Preview of detected silence:")
        for i, interval in enumerate(intervals[:10], 1):
            print(f"  {i}. {interval['start']:.2f}s - {interval['end']:.2f}s ({interval['duration']:.2f}s)")

        if len(intervals) > 10:
            print(f"  ... and {len(intervals) - 10} more")

        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
