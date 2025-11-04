#!/usr/bin/env python3
"""Extract high-quality MP3 audio from the main video file in the workspace.

Usage:
    python extract_audio_from_video.py [input_video] [output_mp3]

If paths are omitted the script looks for the single MP4/MKV/WEBM file in the
current directory (workspace) and produces `<video_basename>.mp3` at 320 kbps.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

SUPPORTED_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi")


def find_video_file() -> Path:
    candidates = [p for p in Path.cwd().iterdir() if p.suffix.lower() in SUPPORTED_VIDEO_EXTS and p.is_file()]
    if not candidates:
        raise FileNotFoundError("No video files (.mp4/.mov/.mkv/.webm/.avi) found in current directory.")
    if len(candidates) > 1:
        raise ValueError("Multiple video files found. Specify one explicitly.")
    return candidates[0]


def build_output_path(input_path: Path, explicit: Optional[str]) -> Path:
    if explicit:
        output = Path(explicit).expanduser()
        if output.is_dir():
            return output / (input_path.stem + ".mp3")
        return output
    return input_path.with_suffix(".mp3")


def extract_audio(input_path: Path, output_path: Path) -> None:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "320k",
        "-ar",
        "48000",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not installed or not in PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract high-quality MP3 audio from a video file.")
    parser.add_argument("input", nargs="?", help="Path to the video file")
    parser.add_argument("output", nargs="?", help="Destination MP3 file (optional)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        input_path = Path(args.input).expanduser() if args.input else find_video_file()
        if not input_path.is_file():
            raise FileNotFoundError(f"Video file not found: {input_path}")
        output_path = build_output_path(input_path, args.output)
        print(f"Extracting audio from {input_path.name} → {output_path.name} at 320 kbps…")
        extract_audio(input_path, output_path)
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"✅ Audio saved to {output_path} ({size_mb:.1f} MB)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
