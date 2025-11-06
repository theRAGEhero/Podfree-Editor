#!/usr/bin/env python3
"""
Generate a point-by-point YouTube-ready summary from a transcript using Groq.

Example:
    python summarization.py alessandro-oppos-studio_alex-alessandro.txt -o summarization.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import textwrap
from typing import Final, Optional

import requests


DEFAULT_MODEL: Final[str] = "llama-3.1-8b-instant"
DEFAULT_API_BASE: Final[str] = "https://api.groq.com/openai/v1"
MAX_PROMPT_TOKENS: Final[int] = 6_000  # keep well under free tier TPM (6k) and context limit
MAX_COMPLETION_TOKENS: Final[int] = 512
CHARS_PER_TOKEN: Final[float] = 4.0  # rough heuristic for English text

logger = logging.getLogger("summarization")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a bullet-point YouTube summary from a transcript via Groq."
    )
    parser.add_argument(
        "input_file",
        help="Path to the transcript .txt file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="summarization.txt",
        help="Output file for the summary (default: summarization.txt).",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="Optional Groq API base URL. Defaults to GROQ_API_BASE env var or Groq public endpoint.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Groq model to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Groq API key. Defaults to GROQ_API_KEY env var or value in --env-file.",
    )
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=MAX_COMPLETION_TOKENS,
        help=f"Maximum tokens in the summary response (default: {MAX_COMPLETION_TOKENS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and exit without calling Groq.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional path to a .env file containing GROQ_API_KEY and related settings.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
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


def load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        logger.warning("Failed to read env file %s: %s", path, exc)


def read_transcript(path: str) -> str:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(int(len(text) / CHARS_PER_TOKEN), 0)


def chunk_transcript(transcript: str, max_prompt_tokens: int) -> list[str]:
    if estimate_tokens(transcript) <= max_prompt_tokens:
        return [transcript]

    max_chars_per_chunk = int(max_prompt_tokens * CHARS_PER_TOKEN * 0.9)
    chunks: list[str] = []
    start = 0
    while start < len(transcript):
        end = start + max_chars_per_chunk
        chunks.append(transcript[start:end])
        start = end
    return chunks


def build_system_prompt() -> str:
    return (
        "You are an expert podcast producer who creates concise yet detailed summaries "
        "for YouTube video descriptions. Maintain factual accuracy, neutral tone, and "
        "use bullet points with short headers plus explanations."
    )


def build_user_prompt(transcript: str) -> str:
    return textwrap.dedent(
        f"""
        Summarize the following transcript into a YouTube description with clear bullet points.

        Requirements:
        * Provide 5-8 bullet points, each starting with a bolded short title followed by a colon.
        * Highlight the key ideas, insights, or takeaways in chronological order.
        * Mention notable quotes or examples only if they reinforce the main story.
        * Keep language concise and professional; avoid promotional hype.
        * Add a closing line suggesting who would benefit from watching the full episode.

        Transcript:
        \"\"\"{transcript}\"\"\"
        """
    ).strip()


def call_groq(
    *,
    api_base: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_completion_tokens: int,
) -> str:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "max_tokens": max_completion_tokens,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )

    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Groq API error (status {response.status_code}): {detail}")

    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("Groq API returned no choices.")
    message = choices[0].get("message")
    if not message:
        raise RuntimeError("Groq API response missing message.")
    content = message.get("content")
    if not content:
        raise RuntimeError("Groq API response missing content.")
    return content.strip()


def summarize_transcript(
    transcript: str,
    *,
    api_base: str,
    api_key: str,
    model: str,
    max_completion_tokens: int,
) -> str:
    chunks = chunk_transcript(transcript, MAX_PROMPT_TOKENS)
    system_prompt = build_system_prompt()

    summaries: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        logger.info("Summarizing chunk %d/%d (~%d tokens).", idx, len(chunks), estimate_tokens(chunk))
        user_prompt = build_user_prompt(chunk)
        summary = call_groq(
            api_base=api_base,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_completion_tokens=max_completion_tokens,
        )
        summaries.append(summary.strip())

    if len(summaries) == 1:
        return summaries[0]

    logger.info("Combining partial summaries into final result.")
    combined_prompt = textwrap.dedent(
        f"""
        Merge the bullet lists below into a single cohesive YouTube summary.
        * Remove redundant bullets.
        * Preserve chronological order.
        * Keep bolded headers distinct and avoid duplication.

        Partial summaries:
        \"\"\"{"\n\n".join(summaries)}\"\"\"
        """
    ).strip()

    return call_groq(
        api_base=api_base,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_prompt=combined_prompt,
        max_completion_tokens=max_completion_tokens,
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    try:
        transcript = read_transcript(args.input_file)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)
    except OSError as exc:
        logger.exception("Failed to read transcript.")
        sys.stderr.write(f"Failed to read input file: {exc}\n")
        sys.exit(1)

    if not transcript.strip():
        sys.stderr.write("Transcript is empty; nothing to summarize.\n")
        sys.exit(1)

    load_env_file(args.env_file)

    api_base = args.api_base or os.environ.get("GROQ_API_BASE", DEFAULT_API_BASE)
    api_key = args.api_key or os.environ.get("GROQ_API_KEY")

    if args.dry_run:
        logger.info("Dry run requested; printing prompts and exiting.")
        prompt = build_user_prompt(transcript if len(transcript) < 4000 else transcript[:4000] + "…")
        print("=== SYSTEM PROMPT ===")
        print(build_system_prompt())
        print("\n=== USER PROMPT (truncated) ===")
        print(prompt)
        return

    if not api_key:
        sys.stderr.write("Missing Groq API key. Provide it via --api-key, GROQ_API_KEY env var, or .env file.\n")
        sys.exit(1)

    try:
        summary = summarize_transcript(
            transcript,
            api_base=api_base,
            api_key=api_key,
            model=args.model,
            max_completion_tokens=args.max_completion_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to summarize transcript.")
        sys.stderr.write(f"Error while summarizing transcript: {exc}\n")
        sys.exit(1)

    output_path = args.output
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(summary.strip() + "\n")
    except OSError as exc:
        logger.exception("Failed to write summary to disk.")
        sys.stderr.write(f"Failed to write output: {exc}\n")
        sys.exit(1)

    print(f"YouTube summary written to: {output_path}")


if __name__ == "__main__":
    main()
def load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        logger.warning("Failed to read env file %s: %s", path, exc)
