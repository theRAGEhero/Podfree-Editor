#!/usr/bin/env python3
"""Podfree local development server.

This server hosts the web UI, exposes helper APIs, and runs automation scripts
against a user-selected workspace folder that contains the episode assets
(audio, video, markdown notes, etc.).
"""

from __future__ import annotations

import argparse
import cgi
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

# Import database module
try:
    from database import UserDB
except ImportError:
    UserDB = None  # Will be None if bcrypt not installed yet

try:
    import markdown as markdown_lib
except ImportError:  # pragma: no cover
    markdown_lib = None

try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:  # pragma: no cover - optional GUI dependency
    tk = None
    filedialog = None

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
SCRIPTS_DIR = ROOT_DIR / "scripts"
PROJECTS_DIR = ROOT_DIR / "projects"
TEMPLATES_DIR = ROOT_DIR / "templates"
DATA_DIR = ROOT_DIR / "data"
TEMPLATES_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
NOTES_TEMPLATE_PATH = TEMPLATES_DIR / "Notes-template.md"
PROJECTS_DIR.mkdir(exist_ok=True)

WORKSPACE_DIR: Optional[Path] = None
_WORKSPACE_CACHE: Optional[Dict[str, Any]] = None

JOB_POLL_INTERVAL = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("podfree")

ENV_PATHS = [
    ROOT_DIR / ".env",
    ROOT_DIR.parent / ".env",
]


def _parse_env_files() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for path in ENV_PATHS:
        if not path or not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, raw_value = stripped.split("=", 1)
                key = key.strip()
                value = raw_value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
                    os.environ.setdefault(key, value)
        except OSError as exc:  # noqa: BLE001
            logger.warning("Unable to read %s: %s", path, exc)
    return values


def load_auth_credentials() -> Tuple[str, str]:
    merged = _parse_env_files()
    username = os.environ.get("PODFREE_USERNAME") or merged.get("PODFREE_USERNAME")
    password = os.environ.get("PODFREE_PASSWORD") or merged.get("PODFREE_PASSWORD")
    if not username or not password:
        raise SystemExit(
            "Authentication unavailable. Set PODFREE_USERNAME and PODFREE_PASSWORD "
            "in the environment or .env file before starting the server."
        )
    return username, password


AUTH_USERNAME, AUTH_PASSWORD = load_auth_credentials()
SESSION_COOKIE_NAME = "podfree_session"
SESSION_TTL_SECONDS = 12 * 60 * 60

logger.info("Authentication enabled for user %s", AUTH_USERNAME)

# Initialize database
user_db = None
if UserDB is not None:
    try:
        DB_PATH = DATA_DIR / "podfree.db"
        user_db = UserDB(DB_PATH)
        logger.info("Database initialized at %s", DB_PATH)
    except Exception as e:
        logger.warning("Database initialization failed: %s (continuing with env-based auth only)", e)
        user_db = None
else:
    logger.warning("Database module not available (bcrypt not installed). Run: pip install bcrypt")



def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\-_]+", "-", name.strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "project"


def safe_filename(name: str) -> str:
    base = Path(name).name
    if not base:
        return "file"
    stem = slugify(Path(base).stem)
    suffix = Path(base).suffix
    return (stem or "file") + suffix


def get_user_projects_dir(username: str) -> Path:
    """Get the projects directory for a specific user."""
    user_dir = PROJECTS_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self, job_type: str, label: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "type": job_type,
                "label": label,
                "status": JobStatus.PENDING,
                "progress": 0.0,
                "message": "queued",
                "logs": [],
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        logger.info("[job %s] created (%s) — %s", job_id[:8], job_type, label)
        return job_id

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        log_line: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if status is not None:
                job["status"] = status
            if progress is not None:
                job["progress"] = max(0.0, min(100.0, progress))
            if message is not None:
                job["message"] = message
            if log_line is not None:
                job.setdefault("logs", []).append(log_line)
            job["updated_at"] = time.time()
        if status is not None:
            logger.info("[job %s] status → %s", job_id[:8], status)
        if message is not None:
            logger.info("[job %s] message → %s", job_id[:8], message)
        if progress is not None:
            logger.debug("[job %s] progress → %.2f%%", job_id[:8], progress)
        if log_line is not None:
            logger.info("[job %s] %s", job_id[:8], log_line)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {job_id: dict(data) for job_id, data in self._jobs.items()}


job_manager = JobManager()


class SessionManager:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _is_expired(self, session: Dict[str, Any]) -> bool:
        if not self.ttl:
            return False
        last_seen = session.get("last_seen", session.get("created", 0))
        return (time.time() - float(last_seen)) > self.ttl

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        payload = {"user": username, "created": time.time(), "last_seen": time.time()}
        with self._lock:
            self._sessions[token] = payload
        return token

    def get_session(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if self._is_expired(session):
                del self._sessions[token]
                return None
            session["last_seen"] = time.time()
            return dict(session)

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def purge_expired(self) -> None:
        if not self.ttl:
            return
        now = time.time()
        with self._lock:
            expired = [token for token, session in self._sessions.items() if (now - session.get("last_seen", now)) > self.ttl]
            for token in expired:
                self._sessions.pop(token, None)


session_manager = SessionManager()


def render_markdown(text: str) -> str:
    if markdown_lib is not None:
        return markdown_lib.markdown(text, extensions=["extra", "sane_lists", "toc"])
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>\n")
    )
    return f"<pre>{escaped}</pre>"


def _empty_summary(path: Optional[Path] = None) -> Dict[str, Any]:
    return {
        "path": str(path) if path else None,
        "valid": False,
        "project_name": None,
        "files": {
            "notes": None,
            "srt": None,
            "video": None,
            "audio": None,
            "proxy": None,
            "transcript_json": None,
            "chapters": None,
        },
        "options": {
            "notes": [],
            "srt": [],
            "video": [],
            "audio": [],
            "transcript_json": [],
            "chapters": [],
        },
        "timestamp": time.time(),
    }

def seed_notes_file(project_dir: Path) -> Optional[Path]:
    """Ensure the project folder has a Notes.md scaffold."""
    if not project_dir.is_dir():
        return None
    for existing in project_dir.glob("Notes*.md"):
        if existing.is_file():
            return existing

    template = NOTES_TEMPLATE_PATH
    if not template.is_file():
        fallback = ROOT_DIR.parent / "Notes.md"
        if fallback.is_file():
            template = fallback
        else:
            return None

    destination = project_dir / "Notes.md"
    if destination.exists():
        return destination

    try:
        content = template.read_text(encoding="utf-8")
        destination.write_text(content, encoding="utf-8")
        logger.info("Seeded Notes.md in %s from template %s", project_dir, template.name)
        return destination
    except OSError as exc:  # noqa: BLE001
        logger.error("Failed to seed Notes.md in %s: %s", project_dir, exc)
        return None


def summarize_directory(ws: Path) -> Dict[str, Any]:
    if not ws.is_dir():
        return _empty_summary(ws)

    seed_notes_file(ws)
    md_candidates = sorted(ws.glob("Notes*.md"))
    if not md_candidates:
        md_candidates = sorted(p for p in ws.glob("*.md") if p.name.lower() != "readme.md")
    notes_name = md_candidates[0].name if md_candidates else None

    def list_files(exts: tuple[str, ...]) -> list[str]:
        return sorted(p.name for p in ws.iterdir() if p.is_file() and p.suffix.lower() in exts)

    srt_files = list_files((".srt", ".vtt"))
    mp4_files = list_files((".mp4", ".mov", ".mkv", ".webm", ".avi"))
    mp3_files = list_files((".mp3", ".wav", ".flac"))
    json_transcripts: list[tuple[int, str]] = []
    for candidate in ws.rglob("*.json"):
        if not candidate.is_file():
            continue
        name = candidate.name.lower()
        if any(keyword in name for keyword in ("transcript", "deliberation", "raw")):
            rel = candidate.relative_to(ws).as_posix()
            priority = 0
            if "deliberation" in name:
                priority = -2
            elif "transcript" in name:
                priority = -1
            elif "raw" in name:
                priority = 0
            else:
                priority = 1
            json_transcripts.append((priority, rel))
    json_transcripts.sort()
    json_transcript_files = [item[1] for item in json_transcripts]

    chapter_files: list[str] = []
    castopod_dir = ws / "Castopod"
    if castopod_dir.is_dir():
        for candidate in sorted(castopod_dir.glob("*.json")):
            if candidate.is_file():
                chapter_files.append(candidate.relative_to(ws).as_posix())

    def is_proxy(name: str) -> bool:
        lowered = name.lower()
        return any(keyword in lowered for keyword in ("proxy", "light", "ultra", "low"))

    video_name = next((name for name in mp4_files if not is_proxy(name)), None)
    if video_name is None and mp4_files:
        video_name = mp4_files[0]

    proxy_name = next((name for name in mp4_files if is_proxy(name) and name != video_name), None)

    has_video = video_name is not None
    project_name: Optional[str] = None
    try:
        rel_project = ws.relative_to(PROJECTS_DIR)
        if rel_project.parts:
            project_name = rel_project.parts[0]
    except ValueError:
        project_name = None

    summary = {
        "path": str(ws),
        "valid": has_video,
        "project_name": project_name,
        "files": {
            "notes": notes_name,
            "srt": srt_files[0] if srt_files else None,
            "video": video_name,
            "audio": mp3_files[0] if mp3_files else None,
            "proxy": proxy_name,
            "transcript_json": json_transcript_files[0] if json_transcript_files else None,
            "chapters": chapter_files[0] if chapter_files else None,
        },
        "options": {
            "notes": [p.name for p in md_candidates],
            "srt": srt_files,
            "video": mp4_files,
            "audio": mp3_files,
            "transcript_json": json_transcript_files,
            "chapters": chapter_files,
        },
        "timestamp": time.time(),
        "missing": {
            "video": not has_video,
            "srt": not srt_files,
            "notes": not md_candidates,
            "audio": not mp3_files,
            "chapters": not chapter_files,
        },
    }
    return summary


def refresh_workspace_cache() -> None:
    if WORKSPACE_DIR is not None:
        scan_workspace(refresh=True)


def _workspace_payload_template() -> Dict[str, Any]:
    return _empty_summary(WORKSPACE_DIR)


def scan_workspace(*, refresh: bool = False) -> Optional[Dict[str, Any]]:
    global _WORKSPACE_CACHE
    if WORKSPACE_DIR is None or not WORKSPACE_DIR.is_dir():
        _WORKSPACE_CACHE = None
        return None
    if _WORKSPACE_CACHE is not None and not refresh:
        return _WORKSPACE_CACHE

    summary = summarize_directory(WORKSPACE_DIR)
    _WORKSPACE_CACHE = summary
    return summary


def list_projects(username: Optional[str] = None) -> Dict[str, Any]:
    """List projects for a specific user, or all projects if username is None."""
    projects = []
    if username:
        # List projects for specific user only
        user_projects_dir = get_user_projects_dir(username)
        for project_dir in sorted(user_projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            summary = summarize_directory(project_dir)
            summary["name"] = project_dir.name
            projects.append(summary)
        return {"projects": projects, "root": str(user_projects_dir)}
    else:
        # List all projects (backward compatibility for env-based users)
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            summary = summarize_directory(project_dir)
            summary["name"] = project_dir.name
            projects.append(summary)
        return {"projects": projects, "root": str(PROJECTS_DIR)}


SCRIPT_LABELS = {
    "deepgram_transcribe_debates.py": "Transcribe with Deepgram",
    "extract_audio_from_video.py": "Extract .mp3 File",
    "detect_silence.py": "Detect Silence",
    "remove_silence.py": "Remove Silence (Render)",
    "generate_covers.py": "Generate Covers",
    "generate_chapters.py": "Generate Chapters",
    "export_castopod_chapters.py": "Export Castopod Chapters",
    "prepare_linkedin_post.py": "Draft LinkedIn Post",
    "create_ghost_post.py": "Post to Ghost CMS",
    "castopod_post.py": "Castopod Draft",
    "post_to_linkedin.py": "Post to LinkedIn",
    "post_to_facebook.py": "Post to Facebook",
    "post_to_twitter.py": "Post to Twitter/X",
    "post_to_mastodon.py": "Post to Mastodon",
    "post_to_bluesky.py": "Post to Bluesky",
    "identify_participants.py": "Identify Participants",
}

SCRIPT_SORT_PRIORITY = {
    "extract_audio_from_video.py": 0,
    "detect_silence.py": 1,
    "remove_silence.py": 2,
    "deepgram_transcribe_debates.py": 3,
    "generate_covers.py": 4,
    "generate_chapters.py": 5,
    "export_castopod_chapters.py": 6,
    "prepare_linkedin_post.py": 7,
    "identify_participants.py": 8,
    "create_ghost_post.py": 9,
    "castopod_post.py": 10,
    "post_to_linkedin.py": 11,
    "post_to_facebook.py": 12,
    "post_to_twitter.py": 13,
    "post_to_mastodon.py": 14,
    "post_to_bluesky.py": 15,
}


def find_notes_file() -> Optional[Path]:
    summary = scan_workspace()
    if not summary:
        return None
    notes_name = summary["files"]["notes"]
    if not notes_name:
        return None
    return WORKSPACE_DIR / notes_name  # type: ignore[arg-type]


def _script_label(name: str) -> str:
    return SCRIPT_LABELS.get(name, name.replace("_", " ").replace(".py", "").title())


def list_scripts_with_status() -> list[Dict[str, Any]]:
    summary = scan_workspace()
    files = summary.get("files") if summary else {}
    options = summary.get("options") if summary else {}
    workspace_path = Path(summary["path"]) if summary and summary.get("path") else None

    def existing_relatives(candidates: Iterable[str]) -> list[str]:
        if not workspace_path:
            return []
        seen: list[str] = []
        for rel in candidates:
            if not rel:
                continue
            candidate = workspace_path / rel
            if candidate.is_file() and rel not in seen:
                seen.append(rel)
        return seen
    scripts: list[Dict[str, Any]] = []
    for script in sorted(SCRIPTS_DIR.rglob("*.py")):
        name = script.name
        relative_path = script.relative_to(SCRIPTS_DIR).as_posix()
        
        # Skip utility files that aren't meant to be run as scripts
        if name in ("__init__.py", "llm_client.py"):
            continue
        
        label = _script_label(name)
        status = "unknown"
        outputs: list[str] = []

        if summary:
            if name == "deepgram_transcribe_debates.py":
                candidates = list(options.get("transcript_json", []))
                transcript_file = files.get("transcript_json")
                if transcript_file and transcript_file not in candidates:
                    candidates.insert(0, transcript_file)
                outputs = existing_relatives(candidates)
                status = "ready" if outputs else "missing"
            elif name == "extract_audio_from_video.py":
                candidates = list(options.get("audio", []))
                audio_file = files.get("audio")
                if audio_file and audio_file not in candidates:
                    candidates.insert(0, audio_file)
                outputs = existing_relatives(candidates)
                status = "ready" if outputs else "missing"
            elif name == "generate_covers.py":
                outputs = existing_relatives(["youtube-cover.jpg", "podcast-cover.jpg"])
                status = "ready" if outputs else "missing"
            elif name == "generate_chapters.py":
                candidates = list(options.get("transcript_json", []))
                transcript_file = files.get("transcript_json")
                if transcript_file and transcript_file not in candidates:
                    candidates.insert(0, transcript_file)
                outputs = existing_relatives(candidates)
                status = "ready" if transcript_file and outputs else "missing"
            elif name == "export_castopod_chapters.py":
                candidates = list(options.get("chapters", []))
                outputs = existing_relatives(candidates)
                status = "ready" if files.get("notes") and outputs else "missing"
            elif name == "prepare_linkedin_post.py":
                notes_exists = bool(files.get("notes"))
                status = "ready" if notes_exists else "missing"
            elif name == "post_to_linkedin.py":
                notes_exists = bool(files.get("notes"))
                status = "ready" if notes_exists else "missing"
            elif name == "identify_participants.py":
                transcript_exists = bool(files.get("transcript_json"))
                notes_exists = bool(files.get("notes"))
                status = "ready" if transcript_exists and notes_exists else "missing"
            elif name == "create_ghost_post.py":
                status = "unknown"

        if outputs:
            deduped: list[str] = []
            for item in outputs:
                if item not in deduped:
                    deduped.append(item)
            outputs = deduped

        scripts.append(
            {
                "name": name,
                "path": relative_path,
                "label": label,
                "status": status,
                "outputs": outputs,
            }
        )
    scripts.sort(
        key=lambda item: (
            SCRIPT_SORT_PRIORITY.get(item["name"], len(SCRIPT_SORT_PRIORITY)),
            item["label"],
        )
    )
    return scripts


def resolve_workspace_path(relative_path: str) -> Path:
    if WORKSPACE_DIR is None:
        raise ValueError("Workspace not set")
    rel = Path(relative_path)
    candidate = (WORKSPACE_DIR / rel).resolve()
    candidate.relative_to(WORKSPACE_DIR)
    return candidate


def workspace_path_from_name(name: str) -> Path:
    if WORKSPACE_DIR is None:
        raise ValueError("Workspace not set")
    rel = Path(name)
    if rel.is_absolute():
        candidate = rel.resolve()
    else:
        candidate = (WORKSPACE_DIR / rel).resolve()
    candidate.relative_to(WORKSPACE_DIR)
    return candidate


def run_ffprobe_duration(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def run_ffmpeg_proxy(job_id: str, source: Path, target: Path) -> None:
    logger.info("[job %s] ffmpeg proxy started (%s → %s)", job_id[:8], source.name, target.name)
    if not source.is_file():
        job_manager.update_job(job_id, status=JobStatus.FAILED, message=f"Source not found: {source}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    duration = run_ffprobe_duration(source)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        "scale=640:-2,fps=15",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "32",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-loglevel",
        "error",
        str(target),
    ]

    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(WORKSPACE_DIR or source.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
    except FileNotFoundError:
        job_manager.update_job(job_id, status=JobStatus.FAILED, message="ffmpeg not installed or not in PATH")
        return

    job_manager.update_job(job_id, status=JobStatus.RUNNING, message="encoding")

    try:
        for line in process.stdout or []:
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_ms=") and duration:
                time_value = line.split("=", 1)[1]
                if time_value != "N/A":
                    try:
                        out_time_ms = float(time_value)
                        progress = (out_time_ms / 1_000_000.0) / duration * 100.0
                        job_manager.update_job(job_id, progress=progress)
                    except ValueError:
                        pass
                continue
            if line.startswith("progress="):
                job_manager.update_job(job_id, message=line.split("=", 1)[1])
                continue
            job_manager.update_job(job_id, log_line=line)
    finally:
        retcode = process.wait()
        if retcode == 0 and target.is_file():
            job_manager.update_job(job_id, status=JobStatus.COMPLETED, progress=100.0, message="proxy ready")
            logger.info("[job %s] ffmpeg proxy completed", job_id[:8])
        else:
            job_manager.update_job(job_id, status=JobStatus.FAILED, message=f"ffmpeg exited with {retcode}")
            logger.error("[job %s] ffmpeg proxy failed (code %s)", job_id[:8], retcode)
        refresh_workspace_cache()


def run_script_job(job_id: str, script: Path, workspace: Path) -> None:
    logger.info("[job %s] script started (%s)", job_id[:8], script.name)
    if not script.is_file():
        job_manager.update_job(job_id, status=JobStatus.FAILED, message=f"Script not found: {script}")
        return

    try:
        process = subprocess.Popen(
            ["python3", str(script)],
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        job_manager.update_job(job_id, status=JobStatus.FAILED, message="python3 not available")
        return

    job_manager.update_job(job_id, status=JobStatus.RUNNING, message="running")

    try:
        for line in process.stdout or []:
            job_manager.update_job(job_id, log_line=line.rstrip())
    finally:
        retcode = process.wait()
        if retcode == 0:
            job_manager.update_job(job_id, status=JobStatus.COMPLETED, progress=100.0, message="completed")
            logger.info("[job %s] script finished successfully", job_id[:8])
        else:
            job_manager.update_job(job_id, status=JobStatus.FAILED, message=f"exit code {retcode}")
            logger.error("[job %s] script failed with exit code %s", job_id[:8], retcode)
        refresh_workspace_cache()


def auto_process_video(project_dir: Path, video_filename: str) -> None:
    """Automatically process uploaded video: create proxy, extract audio, transcribe."""
    logger.info("Auto-processing video: %s in %s", video_filename, project_dir.name)

    # Check if it's a video file
    video_exts = ('.mp4', '.mov', '.mkv', '.webm', '.avi')
    video_path = project_dir / video_filename
    if not video_path.suffix.lower() in video_exts:
        logger.info("Skipping auto-process - not a video file: %s", video_filename)
        return

    # Refresh summary to check what already exists
    summary = summarize_directory(project_dir)

    # 1. Create lightweight video (proxy) if it doesn't exist
    if not summary['files'].get('proxy'):
        logger.info("Creating lightweight video for %s", video_filename)
        # Generate proxy filename
        stem = video_path.stem
        proxy_filename = f"{stem}_proxy{video_path.suffix}"
        proxy_path = project_dir / proxy_filename

        job_id = job_manager.create_job("proxy", f"Auto-create proxy for {video_filename}")
        thread = threading.Thread(
            target=run_ffmpeg_proxy,
            args=(job_id, video_path, proxy_path),
            daemon=True,
        )
        thread.start()
    else:
        logger.info("Proxy video already exists, skipping")

    # 2. Extract mp3 if it doesn't exist
    if not summary['files'].get('audio'):
        logger.info("Extracting audio from %s", video_filename)
        script_path = SCRIPTS_DIR / "editing" / "extract_audio_from_video.py"
        if script_path.is_file():
            job_id = job_manager.create_job("script", "Auto-extract audio")
            thread = threading.Thread(
                target=run_script_job,
                args=(job_id, script_path, project_dir),
                daemon=True,
            )
            thread.start()
        else:
            logger.warning("Audio extraction script not found: %s", script_path)
    else:
        logger.info("Audio file already exists, skipping extraction")

    # 3. Run transcription if JSON doesn't exist
    if not summary['files'].get('transcript_json'):
        logger.info("Scheduling transcription for %s", video_filename)
        script_path = SCRIPTS_DIR / "ai-tools" / "deepgram_transcribe_debates.py"
        if script_path.is_file():
            # Wait a bit for audio extraction to complete before transcription
            # We'll create a delayed job
            def delayed_transcription():
                time.sleep(10)  # Wait 10 seconds for audio extraction
                # Re-check if audio now exists
                updated_summary = summarize_directory(project_dir)
                if updated_summary['files'].get('audio'):
                    job_id = job_manager.create_job("script", "Auto-transcribe with Deepgram")
                    run_script_job(job_id, script_path, project_dir)
                else:
                    logger.warning("Audio file still not available, skipping transcription")

            thread = threading.Thread(target=delayed_transcription, daemon=True)
            thread.start()
        else:
            logger.warning("Transcription script not found: %s", script_path)
    else:
        logger.info("Transcription already exists, skipping")


def build_segments(edited_words: list[Dict[str, Any]]) -> list[Dict[str, float]]:
    """Build time segments for words that should be kept (not deleted)."""
    segments = []
    current_segment = None

    deleted_count = sum(1 for w in edited_words if w.get('deleted', False))
    kept_count = len(edited_words) - deleted_count

    logger.info("Building segments from %d words: %d kept, %d deleted", len(edited_words), kept_count, deleted_count)

    for i, word in enumerate(edited_words):
        is_deleted = word.get('deleted', False)

        if is_deleted:
            # Word is deleted, close current segment if any
            if current_segment:
                segments.append(current_segment)
                logger.debug("Closed segment at word %d: %.3fs - %.3fs", i, current_segment['start'], current_segment['end'])
                current_segment = None
        else:
            # Word is kept
            if current_segment is None:
                # Start new segment
                current_segment = {
                    'start': word['start'],
                    'end': word['end']
                }
                logger.debug("Started new segment at word %d: %.3fs", i, word['start'])
            else:
                # Extend current segment
                current_segment['end'] = word['end']

    # Add final segment if exists
    if current_segment:
        segments.append(current_segment)
        logger.debug("Final segment: %.3fs - %.3fs", current_segment['start'], current_segment['end'])

    logger.info("Built %d segments to keep", len(segments))

    # Log segment summary
    for i, seg in enumerate(segments):
        duration = seg['end'] - seg['start']
        logger.info("  Segment %d: %.3fs - %.3fs (duration: %.3fs)", i+1, seg['start'], seg['end'], duration)

    return segments


def export_video_ffmpeg(job_id: str, video_path: Path, segments: list[Dict[str, float]], output_path: Path) -> bool:
    """Export video using FFmpeg, concatenating segments with re-encoding for reliability."""
    try:
        if not segments:
            logger.warning("[job %s] No segments to export", job_id[:8])
            job_manager.update_job(job_id, status=JobStatus.FAILED, message="No segments to export")
            return False

        logger.info("[job %s] Starting export with %d segments from %s", job_id[:8], len(segments), video_path.name)

        # If output already exists, remove it
        if output_path.exists():
            output_path.unlink()
            logger.info("[job %s] Removed existing output file: %s", job_id[:8], output_path.name)

        # Create temporary directory for segment files
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            segment_files = []

            # Extract each segment with re-encoding for better compatibility
            for i, seg in enumerate(segments):
                segment_file = temp_path / f"segment_{i:04d}.mp4"
                start_time = seg['start']
                duration = seg['end'] - seg['start']

                # Use re-encoding instead of copy for reliable concatenation
                cmd = [
                    'ffmpeg', '-y',
                    '-ss', str(start_time),
                    '-i', str(video_path),
                    '-t', str(duration),
                    '-c:v', 'libx264',
                    '-preset', 'fast',
                    '-crf', '23',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-movflags', '+faststart',
                    str(segment_file)
                ]

                logger.info("[job %s] Extracting segment %d/%d: %.3fs -> %.3fs (duration: %.3fs)",
                           job_id[:8], i+1, len(segments), start_time, seg['end'], duration)
                job_manager.update_job(job_id, progress=(i / len(segments)) * 50,
                                      message=f"Extracting segment {i+1}/{len(segments)}")

                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    logger.error("[job %s] FFmpeg segment extraction error for segment %d:", job_id[:8], i+1)
                    logger.error("[job %s] Command: %s", job_id[:8], ' '.join(cmd))
                    logger.error("[job %s] Stderr: %s", job_id[:8], result.stderr)
                    continue

                # Verify segment was created
                if segment_file.exists() and segment_file.stat().st_size > 0:
                    segment_files.append(segment_file)
                    logger.info("[job %s] ✓ Segment %d created: %.1f KB",
                               job_id[:8], i+1, segment_file.stat().st_size / 1024)
                else:
                    logger.error("[job %s] ✗ Segment %d was not created or is empty", job_id[:8], i+1)

            if not segment_files:
                logger.error("[job %s] No segments were successfully extracted", job_id[:8])
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Failed to extract segments")
                return False

            logger.info("[job %s] Successfully extracted %d/%d segments",
                       job_id[:8], len(segment_files), len(segments))

            # Create concat file with proper format
            concat_file = temp_path / 'concat.txt'
            with open(concat_file, 'w') as f:
                for seg_file in segment_files:
                    # Use absolute path for concat
                    f.write(f"file '{seg_file.absolute()}'\n")

            logger.info("[job %s] Created concat file with %d entries", job_id[:8], len(segment_files))
            job_manager.update_job(job_id, progress=60, message="Concatenating segments")

            # Concatenate segments
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c', 'copy',
                str(output_path)
            ]

            logger.info("[job %s] Concatenating segments into %s", job_id[:8], output_path.name)
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                logger.error("[job %s] FFmpeg concat error:", job_id[:8])
                logger.error("[job %s] Command: %s", job_id[:8], ' '.join(cmd))
                logger.error("[job %s] Stderr: %s", job_id[:8], result.stderr)
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Concatenation failed")
                return False

            # Verify output file was created
            if not output_path.exists():
                logger.error("[job %s] Output file was not created: %s", job_id[:8], output_path)
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Output file not created")
                return False

            file_size = output_path.stat().st_size
            logger.info("[job %s] ✓ Export completed successfully: %s (%.2f MB)",
                       job_id[:8], output_path.name, file_size / (1024*1024))
            job_manager.update_job(job_id, status=JobStatus.COMPLETED, progress=100.0, message="Export completed")
            return True

    except Exception as e:
        logger.error("[job %s] Error exporting video: %s", job_id[:8], e, exc_info=True)
        job_manager.update_job(job_id, status=JobStatus.FAILED, message=str(e))
        return False


def export_audio_ffmpeg(job_id: str, source_path: Path, segments: list[Dict[str, float]], output_path: Path) -> bool:
    """Export audio using FFmpeg, concatenating segments."""
    try:
        if not segments:
            logger.warning("[job %s] No segments to export", job_id[:8])
            job_manager.update_job(job_id, status=JobStatus.FAILED, message="No segments to export")
            return False

        logger.info("[job %s] Starting audio export with %d segments from %s",
                   job_id[:8], len(segments), source_path.name)

        # If output already exists, remove it
        if output_path.exists():
            output_path.unlink()
            logger.info("[job %s] Removed existing output file: %s", job_id[:8], output_path.name)

        # Create temporary directory for segment files
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            segment_files = []

            # Extract each audio segment
            for i, seg in enumerate(segments):
                segment_file = temp_path / f"segment_{i:04d}.mp3"
                start_time = seg['start']
                duration = seg['end'] - seg['start']

                cmd = [
                    'ffmpeg', '-y',
                    '-ss', str(start_time),
                    '-i', str(source_path),
                    '-t', str(duration),
                    '-vn',  # No video
                    '-c:a', 'libmp3lame',
                    '-b:a', '192k',
                    str(segment_file)
                ]

                logger.info("[job %s] Extracting audio segment %d/%d: %.3fs -> %.3fs",
                           job_id[:8], i+1, len(segments), start_time, seg['end'])
                job_manager.update_job(job_id, progress=(i / len(segments)) * 50,
                                      message=f"Extracting segment {i+1}/{len(segments)}")

                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    logger.error("[job %s] FFmpeg audio extraction error for segment %d", job_id[:8], i+1)
                    logger.error("[job %s] Stderr: %s", job_id[:8], result.stderr)
                    continue

                if segment_file.exists() and segment_file.stat().st_size > 0:
                    segment_files.append(segment_file)
                    logger.info("[job %s] ✓ Audio segment %d created: %.1f KB",
                               job_id[:8], i+1, segment_file.stat().st_size / 1024)
                else:
                    logger.error("[job %s] ✗ Audio segment %d was not created or is empty", job_id[:8], i+1)

            if not segment_files:
                logger.error("[job %s] No audio segments were successfully extracted", job_id[:8])
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Failed to extract segments")
                return False

            logger.info("[job %s] Successfully extracted %d/%d audio segments",
                       job_id[:8], len(segment_files), len(segments))

            # Create concat file
            concat_file = temp_path / 'concat.txt'
            with open(concat_file, 'w') as f:
                for seg_file in segment_files:
                    f.write(f"file '{seg_file.absolute()}'\n")

            logger.info("[job %s] Created concat file with %d entries", job_id[:8], len(segment_files))
            job_manager.update_job(job_id, progress=60, message="Concatenating audio segments")

            # Concatenate audio segments
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c', 'copy',
                str(output_path)
            ]

            logger.info("[job %s] Concatenating audio segments into %s", job_id[:8], output_path.name)
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                logger.error("[job %s] FFmpeg audio concat error:", job_id[:8])
                logger.error("[job %s] Stderr: %s", job_id[:8], result.stderr)
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Concatenation failed")
                return False

            if not output_path.exists():
                logger.error("[job %s] Output file was not created: %s", job_id[:8], output_path)
                job_manager.update_job(job_id, status=JobStatus.FAILED, message="Output file not created")
                return False

            file_size = output_path.stat().st_size
            logger.info("[job %s] ✓ Audio export completed successfully: %s (%.2f MB)",
                       job_id[:8], output_path.name, file_size / (1024*1024))
            job_manager.update_job(job_id, status=JobStatus.COMPLETED, progress=100.0, message="Export completed")
            return True

    except Exception as e:
        logger.error("[job %s] Error exporting audio: %s", job_id[:8], e, exc_info=True)
        job_manager.update_job(job_id, status=JobStatus.FAILED, message=str(e))
        return False


class PodfreeRequestHandler(SimpleHTTPRequestHandler):
    server_version = "Podfree/1.0"
    protocol_version = "HTTP/1.1"
    current_session: Optional[Dict[str, Any]] = None

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        if parsed_path.startswith("/workspace/"):
            if WORKSPACE_DIR is None:
                return super().translate_path("/404")
            rel = unquote(parsed_path[len("/workspace/"):])
            rel_path = Path(rel)
            if rel_path.is_absolute():
                rel_path = rel_path.relative_to(rel_path.anchor)
            candidate = (WORKSPACE_DIR / rel_path).resolve()
            try:
                candidate.relative_to(WORKSPACE_DIR)
            except ValueError:
                return super().translate_path("/403")
            return str(candidate)
        return super().translate_path(path)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _read_cookies(self) -> SimpleCookie:
        cookie_header = self.headers.get("Cookie")
        jar = SimpleCookie()
        if not cookie_header:
            return jar
        try:
            jar.load(cookie_header)
        except Exception:  # noqa: BLE001
            return SimpleCookie()
        return jar

    def _session_token(self) -> Optional[str]:
        jar = self._read_cookies()
        morsel = jar.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _refresh_session(self) -> Optional[Dict[str, Any]]:
        session_manager.purge_expired()
        token = self._session_token()
        session = session_manager.get_session(token)
        self.current_session = session
        return session

    def _set_session_cookie(self, token: str) -> None:
        directives = [
            f"{SESSION_COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if SESSION_TTL_SECONDS:
            directives.append(f"Max-Age={SESSION_TTL_SECONDS}")
        self.send_header("Set-Cookie", "; ".join(directives))

    def _clear_session_cookie(self) -> None:
        directives = [
            f"{SESSION_COOKIE_NAME}=deleted",
            "Path=/",
            "Max-Age=0",
            "SameSite=Lax",
            "HttpOnly",
        ]
        self.send_header("Set-Cookie", "; ".join(directives))

    def _is_public_path(self, path: str) -> bool:
        if path in {"/login", "/login/", "/login.html", "/register", "/register/", "/register.html", "/about.html"}:
            return True
        if path in {"/favicon.ico", "/favicon.png", "/logo.svg"}:
            return True
        if path.startswith("/api/login") or path.startswith("/api/register") or path.startswith("/api/contact"):
            return True
        if path.startswith("/api/") or path.startswith("/workspace/"):
            return False
        ext = Path(path).suffix.lower()
        if ext in {".css", ".js", ".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}:
            return True
        return False

    def _redirect_to_login(self, *, include_next: bool = True) -> None:
        target = "/login.html"
        if include_next and self.path not in {"/", "/login", "/login.html"}:
            next_param = quote(self.path, safe="")
            target = f"/login.html?next={next_param}"
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def _handle_unauthorized(self, path: str) -> None:
        if path.startswith("/api/"):
            self._send_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
        else:
            self._redirect_to_login(include_next=True)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        session = self._refresh_session()
        authenticated = session is not None

        if path in {"/login", "/login.html"} and authenticated:
            self.send_response(302)
            self.send_header("Location", "/projects.html")
            self.end_headers()
            return

        if path in {"", "/"}:
            if authenticated:
                self.send_response(302)
                self.send_header("Location", "/projects.html")
                self.end_headers()
            else:
                self._redirect_to_login(include_next=False)
            return

        if not authenticated and not self._is_public_path(path):
            self._handle_unauthorized(path)
            return

        if path == "//api/workspace/files":
            path = "/api/workspace/files"

        if path == "/api/workspace/files":
            if WORKSPACE_DIR is None or not WORKSPACE_DIR.is_dir():
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return
            params = parse_qs(parsed.query)
            directory = params.get("dir", ["."])[0].strip() or "."
            try:
                target_dir = resolve_workspace_path(directory)
            except ValueError:
                self._send_json({"error": "invalid directory"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not target_dir.is_dir():
                self._send_json({"error": "path is not a directory"}, status=HTTPStatus.BAD_REQUEST)
                return

            entries: list[Dict[str, Any]] = []
            for candidate in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    stat_info = candidate.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": candidate.name,
                        "path": candidate.relative_to(WORKSPACE_DIR).as_posix(),
                        "is_dir": candidate.is_dir(),
                        "size": stat_info.st_size if candidate.is_file() else None,
                        "modified": stat_info.st_mtime,
                    }
                )

            rel_path = target_dir.relative_to(WORKSPACE_DIR).as_posix()
            current_dir = "." if rel_path == "." else rel_path
            breadcrumbs: list[Dict[str, str]] = [{"label": "Workspace", "path": "."}]
            if current_dir != ".":
                accumulated: list[str] = []
                for part in Path(current_dir).parts:
                    accumulated.append(part)
                    breadcrumbs.append({"label": part, "path": "/".join(accumulated)})

            summary = scan_workspace()
            project_name = summary.get("project_name") if summary else None

            self._send_json(
                {
                    "directory": current_dir,
                    "entries": entries,
                    "breadcrumbs": breadcrumbs,
                    "project_name": project_name,
                }
            )
            return

        if path == "/api/workspace":
            refresh = parse_qs(parsed.query).get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            summary = scan_workspace(refresh=refresh)
            if summary is None:
                summary = _workspace_payload_template()
            if refresh:
                logger.info("Workspace rescan requested")
            self._send_json(summary)
            return

        if path == "/api/scripts":
            self._send_json({"scripts": list_scripts_with_status()})
            return

        if path.startswith("/api/jobs/"):
            job_id = path.split("/", 3)[3] if path.count("/") >= 3 else ""
            job = job_manager.get_job(job_id)
            if not job:
                self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(job)
            return

        if path == "/api/jobs":
            self._send_json(job_manager.list_jobs())
            return

        if path == "/api/notes":
            notes_path = find_notes_file()
            if not notes_path:
                self._send_json({"error": "Notes file not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                content = notes_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.error("Failed to read notes file: %s", exc)
                self._send_json({"error": f"Unable to read notes ({exc})"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            html = render_markdown(content)
            self._send_json(
                {
                    "filename": notes_path.name,
                    "path": str(notes_path),
                    "content": content,
                    "html": html,
                    "mtime": os.path.getmtime(notes_path),
                }
            )
            return

        if path == "/api/render_markdown":
            self._send_json({"error": "POST required"}, status=HTTPStatus.METHOD_NOT_ALLOWED)
            return

        if path == "/api/projects":
            # Get username from session for user isolation
            username = session.get("user") if session else None
            self._send_json(list_projects(username))
            return

        if path == "/api/load-transcript-edits":
            if user_db is None:
                # Return empty edits if database not available
                self._send_json({"deletedIndices": []})
                return

            # Get user info from session
            username = session.get("user") if session else None
            if not username:
                # Return empty edits if not authenticated
                self._send_json({"deletedIndices": []})
                return

            user = user_db.get_user_by_username(username)
            if not user:
                self._send_json({"deletedIndices": []})
                return

            params = parse_qs(parsed.query)
            project_name = params.get("projectName", [None])[0]
            transcript_file = params.get("transcriptFile", [None])[0]

            if not project_name or not transcript_file:
                self._send_json({"deletedIndices": []})
                return

            try:
                edits_json = user_db.load_transcript_edits(
                    user_id=user['id'],
                    project_name=project_name,
                    transcript_file=transcript_file
                )

                if edits_json:
                    edits_data = json.loads(edits_json)
                    self._send_json(edits_data)
                else:
                    self._send_json({"deletedIndices": []})
            except Exception as e:
                logger.error("Failed to load transcript edits: %s", e)
                self._send_json({"deletedIndices": []})
            return

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        global WORKSPACE_DIR, _WORKSPACE_CACHE

        session = self._refresh_session()
        authenticated = session is not None

        if path == "/api/login":
            data = self._read_json_body()
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""

            # Try env-based auth first (backward compatibility)
            authenticated = False
            if username == AUTH_USERNAME and password == AUTH_PASSWORD:
                authenticated = True

            # If not env-based, try database auth
            if not authenticated and user_db is not None:
                user = user_db.authenticate(username, password)
                if user:
                    authenticated = True

            if authenticated:
                token = session_manager.create_session(username)
                payload = {"status": "ok"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._set_session_cookie(token)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                payload = {"error": "invalid credentials"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self._clear_session_cookie()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if path == "/api/register":
            # Check if database is available
            if user_db is None:
                self._send_json(
                    {"success": False, "error": "Registration not available (database not initialized)"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE
                )
                return

            data = self._read_json_body()
            username = (data.get("username") or "").strip()
            email = (data.get("email") or "").strip()
            password = data.get("password") or ""

            # Validate input
            if not username or len(username) < 3 or len(username) > 20:
                self._send_json(
                    {"success": False, "error": "Username must be 3-20 characters"},
                    status=HTTPStatus.BAD_REQUEST
                )
                return

            if not re.match(r"^[a-zA-Z0-9_]+$", username):
                self._send_json(
                    {"success": False, "error": "Username can only contain letters, numbers, and underscores"},
                    status=HTTPStatus.BAD_REQUEST
                )
                return

            if not email or "@" not in email or "." not in email:
                self._send_json(
                    {"success": False, "error": "Valid email address required"},
                    status=HTTPStatus.BAD_REQUEST
                )
                return

            if not password or len(password) < 8:
                self._send_json(
                    {"success": False, "error": "Password must be at least 8 characters"},
                    status=HTTPStatus.BAD_REQUEST
                )
                return

            # Try to create user
            try:
                user_id = user_db.create_user(username, email, password)
                logger.info("New user registered: %s (ID: %d)", username, user_id)

                # Get user credits
                credits = user_db.get_user_credits(user_id)

                self._send_json({
                    "success": True,
                    "message": "Registration successful",
                    "user": {
                        "username": username,
                        "email": email,
                        "credits_hours": credits
                    }
                })
            except ValueError as e:
                self._send_json(
                    {"success": False, "error": str(e)},
                    status=HTTPStatus.CONFLICT
                )
            except Exception as e:
                logger.error("Registration failed: %s", e)
                self._send_json(
                    {"success": False, "error": "Registration failed. Please try again."},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR
                )
            return

        if path == "/api/logout":
            token = self._session_token()
            session_manager.destroy_session(token)
            payload = {"status": "logged_out"}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._clear_session_cookie()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not authenticated and not self._is_public_path(path):
            self._handle_unauthorized(path)
            return

        if path == "/api/workspace/delete-files":
            if WORKSPACE_DIR is None or not WORKSPACE_DIR.is_dir():
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return
            data = self._read_json_body()
            paths = data.get("paths")
            if not isinstance(paths, list) or not paths:
                self._send_json({"error": "paths array required"}, status=HTTPStatus.BAD_REQUEST)
                return
            deleted: list[str] = []
            for raw in paths:
                rel = str(raw).strip()
                if not rel or rel == ".":
                    continue
                try:
                    candidate = resolve_workspace_path(rel)
                except ValueError:
                    self._send_json({"error": f"invalid path: {rel}"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    if candidate.is_dir():
                        shutil.rmtree(candidate)
                    elif candidate.is_file():
                        candidate.unlink()
                    else:
                        continue
                    deleted.append(rel)
                except OSError as exc:
                    logger.error("Failed to delete %s: %s", candidate, exc)
                    self._send_json({"error": f"failed to delete {rel}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
            refresh_workspace_cache()
            self._send_json({"status": "deleted", "paths": deleted})
            return

        if path == "/api/workspace/download-zip":
            if WORKSPACE_DIR is None or not WORKSPACE_DIR.is_dir():
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return
            data = self._read_json_body()
            paths = data.get("paths")
            if not isinstance(paths, list) or not paths:
                self._send_json({"error": "paths array required"}, status=HTTPStatus.BAD_REQUEST)
                return

            buffer = io.BytesIO()
            added = 0
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for raw in paths:
                    rel = str(raw).strip()
                    if not rel or rel == ".":
                        continue
                    try:
                        candidate = resolve_workspace_path(rel)
                    except ValueError:
                        self._send_json({"error": f"invalid path: {rel}"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    if candidate.is_file():
                        arcname = candidate.relative_to(WORKSPACE_DIR).as_posix()
                        archive.write(candidate, arcname)
                        added += 1
                    elif candidate.is_dir():
                        for root, _dirs, files in os.walk(candidate):
                            root_path = Path(root)
                            for name in files:
                                file_path = root_path / name
                                arcname = file_path.relative_to(WORKSPACE_DIR).as_posix()
                                archive.write(file_path, arcname)
                                added += 1
            if added == 0:
                self._send_json({"error": "no files selected"}, status=HTTPStatus.BAD_REQUEST)
                return

            buffer.seek(0)
            payload = buffer.getvalue()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="workspace-files.zip"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/workspace":
            data = self._read_json_body()
            raw_path = data.get("path")
            if not raw_path:
                self._send_json({"error": "path required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                candidate = Path(raw_path).expanduser().resolve()
            except OSError as exc:
                self._send_json({"error": f"invalid path: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not candidate.is_dir():
                self._send_json({"error": "directory not found"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Validate user can only access their own projects
            username = session.get("user") if session else None
            if username:
                user_projects_dir = get_user_projects_dir(username)
                try:
                    candidate.relative_to(user_projects_dir)
                except ValueError:
                    self._send_json(
                        {"error": "Access denied - workspace must be within your projects directory"},
                        status=HTTPStatus.FORBIDDEN
                    )
                    return

            WORKSPACE_DIR = candidate
            _WORKSPACE_CACHE = None
            logger.info("Workspace set via API to %s", candidate)
            summary = scan_workspace(refresh=True) or _workspace_payload_template()
            self._send_json(summary)
            return

        if path == "/api/workspace/dialog":
            if filedialog is None:
                self._send_json({"error": "Folder picker not available on this system."}, status=HTTPStatus.NOT_IMPLEMENTED)
                return
            def pick_directory() -> Optional[str]:
                root = tk.Tk()
                root.withdraw()
                try:
                    selected = filedialog.askdirectory(title="Select workspace folder")
                finally:
                    root.destroy()
                return selected or None

            # Run dialog in the same thread (blocking) is fine because the request waits
            try:
                selected_path = pick_directory()
            except Exception as exc:  # noqa: BLE001
                logger.error("Directory picker failed: %s", exc)
                self._send_json({"error": f"Unable to open folder picker: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if not selected_path:
                logger.info("Workspace browse cancelled")
                self._send_json({"path": None, "status": "cancelled"})
                return

            logger.info("Workspace browse selected %s", selected_path)
            self._send_json({"path": selected_path, "status": "ok"})
            return

        if path == "/api/projects":
            data = self._read_json_body()
            name = data.get("name")
            if not name or not str(name).strip():
                self._send_json({"error": "project name required"}, status=HTTPStatus.BAD_REQUEST)
                return
            slug = slugify(str(name))

            # Get username from session for user isolation
            username = session.get("user") if session else None
            if username:
                user_projects_dir = get_user_projects_dir(username)
                project_dir = (user_projects_dir / slug).resolve()
                try:
                    project_dir.relative_to(user_projects_dir)
                except ValueError:
                    self._send_json({"error": "invalid project name"}, status=HTTPStatus.BAD_REQUEST)
                    return
            else:
                # Backward compatibility for env-based users
                project_dir = (PROJECTS_DIR / slug).resolve()
                try:
                    project_dir.relative_to(PROJECTS_DIR)
                except ValueError:
                    self._send_json({"error": "invalid project name"}, status=HTTPStatus.BAD_REQUEST)
                    return
            project_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Project created: %s", project_dir)
            summary = summarize_directory(project_dir)
            summary["name"] = project_dir.name
            self._send_json({"status": "created", "project": summary})
            return

        if path == "/api/projects/upload":
            params = parse_qs(parsed.query)
            project_name = params.get("project", [None])[0]
            if not project_name:
                self._send_json({"error": "project parameter required"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Get username from session for user isolation
            username = session.get("user") if session else None
            if username:
                user_projects_dir = get_user_projects_dir(username)
                project_dir = (user_projects_dir / project_name).resolve()
                try:
                    project_dir.relative_to(user_projects_dir)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return
            else:
                # Backward compatibility for env-based users
                project_dir = (PROJECTS_DIR / project_name).resolve()
                try:
                    project_dir.relative_to(PROJECTS_DIR)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return
            if not project_dir.is_dir():
                self._send_json({"error": "project not found"}, status=HTTPStatus.NOT_FOUND)
                return

            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._send_json({"error": "multipart/form-data required"}, status=HTTPStatus.BAD_REQUEST)
                return

            environ = {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            }
            try:
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)
            except Exception as exc:  # noqa: BLE001
                logger.error("Upload parsing failed: %s", exc)
                self._send_json({"error": f"failed to parse upload: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not getattr(form, "list", None):
                self._send_json({"error": "no files provided"}, status=HTTPStatus.BAD_REQUEST)
                return

            saved = []
            for field in form.list:
                if not getattr(field, "filename", None):
                    continue
                filename = safe_filename(field.filename)
                dest = project_dir / filename
                try:
                    with open(dest, "wb") as fh:
                        data = field.file.read()
                        fh.write(data)
                    saved.append(filename)
                    logger.info("Uploaded %s to project %s", filename, project_name)
                except OSError as exc:
                    logger.error("Failed to save upload %s: %s", filename, exc)
                    self._send_json({"error": f"failed to save {filename}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return

            # Trigger auto-processing for each uploaded video file
            for filename in saved:
                threading.Thread(
                    target=auto_process_video,
                    args=(project_dir, filename),
                    daemon=True,
                ).start()

            summary = summarize_directory(project_dir)
            summary["name"] = project_dir.name
            self._send_json({"status": "uploaded", "saved": saved, "project": summary})
            return

        if path == "/api/projects/download":
            params = parse_qs(parsed.query)
            project_name = params.get("project", [None])[0]
            if not project_name:
                self._send_json({"error": "project parameter required"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Get username from session for user isolation
            username = session.get("user") if session else None
            if username:
                user_projects_dir = get_user_projects_dir(username)
                project_dir = (user_projects_dir / project_name).resolve()
                try:
                    project_dir.relative_to(user_projects_dir)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return
            else:
                # Backward compatibility for env-based users
                project_dir = (PROJECTS_DIR / project_name).resolve()
                try:
                    project_dir.relative_to(PROJECTS_DIR)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return
            if not project_dir.is_dir():
                self._send_json({"error": "project not found"}, status=HTTPStatus.NOT_FOUND)
                return

            buffer = io.BytesIO()
            try:
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for root, _dirs, files in os.walk(project_dir):
                        root_path = Path(root)
                        for filename in files:
                            file_path = root_path / filename
                            rel_path = file_path.relative_to(project_dir)
                            archive.write(file_path, rel_path.as_posix())
                buffer.seek(0)
            except OSError as exc:
                logger.error("Failed to bundle project %s: %s", project_name, exc)
                self._send_json({"error": f"unable to create archive: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            payload = buffer.getvalue()
            archive_name = f"{project_dir.name}.zip"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{archive_name}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/projects/delete":
            data = self._read_json_body()
            name = data.get("name")
            if not name:
                self._send_json({"error": "project name required"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Get username from session for user isolation
            username = session.get("user") if session else None
            if username:
                user_projects_dir = get_user_projects_dir(username)
                project_dir = (user_projects_dir / name).resolve()
                try:
                    project_dir.relative_to(user_projects_dir)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return
            else:
                # Backward compatibility for env-based users
                project_dir = (PROJECTS_DIR / name).resolve()
                try:
                    project_dir.relative_to(PROJECTS_DIR)
                except ValueError:
                    self._send_json({"error": "invalid project"}, status=HTTPStatus.BAD_REQUEST)
                    return

            if not project_dir.exists():
                self._send_json({"status": "not_found"})
                return

            try:
                shutil.rmtree(project_dir)
                logger.info("Project deleted: %s", project_dir)
            except OSError as exc:
                logger.error("Failed to delete project %s: %s", project_dir, exc)
                self._send_json({"error": f"Unable to delete project: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if WORKSPACE_DIR and WORKSPACE_DIR == project_dir:
                logger.info("Workspace cleared because project %s was deleted", name)
                WORKSPACE_DIR = None
                _WORKSPACE_CACHE = None
            refresh_workspace_cache()
            self._send_json({"status": "deleted"})
            return

        if path == "/api/create-proxy":
            if WORKSPACE_DIR is None:
                logger.warning("Proxy creation requested without workspace")
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return
            data = self._read_json_body()
            source_name = data.get("source")
            target_name = data.get("target")
            if not source_name or not target_name:
                self._send_json({"error": "source and target required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                source_path = workspace_path_from_name(source_name)
                target_path = workspace_path_from_name(target_name)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if target_path.exists():
                self._send_json({"job_id": None, "status": "exists"})
                return

            job_id = job_manager.create_job("proxy", f"Create {target_path.name}")
            thread = threading.Thread(target=run_ffmpeg_proxy, args=(job_id, source_path, target_path), daemon=True)
            thread.start()
            self._send_json({"job_id": job_id})
            return

        if path == "/api/run-script":
            if WORKSPACE_DIR is None:
                logger.warning("Script run requested without workspace")
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return
            data = self._read_json_body()
            script_name = data.get("script")
            if not script_name:
                self._send_json({"error": "script required"}, status=HTTPStatus.BAD_REQUEST)
                return

            script_path = (SCRIPTS_DIR / script_name).resolve()
            try:
                script_path.relative_to(SCRIPTS_DIR)
            except ValueError:
                self._send_json({"error": "invalid script"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not script_path.suffix == ".py" or not script_path.is_file():
                self._send_json({"error": "script not found"}, status=HTTPStatus.NOT_FOUND)
                return

            job_id = job_manager.create_job("script", f"Run {script_path.name}")
            thread = threading.Thread(
                target=run_script_job,
                args=(job_id, script_path, WORKSPACE_DIR),
                daemon=True,
            )
            thread.start()
            self._send_json({"job_id": job_id})
            return

        if path == "/api/notes":
            notes_path = find_notes_file()
            if not notes_path:
                self._send_json({"error": "Notes file not found"}, status=HTTPStatus.NOT_FOUND)
                return
            data = self._read_json_body()
            content = data.get("content")
            if content is None:
                self._send_json({"error": "content required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                notes_path.write_text(content, encoding="utf-8")
            except OSError as exc:
                logger.error("Failed to write notes file: %s", exc)
                self._send_json({"error": f"Unable to save notes: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            refresh_workspace_cache()
            self._send_json(
                {
                    "status": "saved",
                    "filename": notes_path.name,
                    "mtime": os.path.getmtime(notes_path),
                }
            )
            return

        if path == "/api/render_markdown":
            data = self._read_json_body()
            content = data.get("content", "")
            html = render_markdown(content)
            self._send_json({"html": html})
            return

        if path == "/api/export-video":
            if WORKSPACE_DIR is None:
                logger.warning("Video export requested without workspace")
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return

            data = self._read_json_body()
            video_file = data.get("videoFile")
            edited_words = data.get("editedWords", [])

            if not video_file:
                self._send_json({"error": "videoFile required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not edited_words:
                self._send_json({"error": "editedWords array required"}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                video_path = workspace_path_from_name(video_file)
            except Exception as exc:
                logger.error("Invalid video file path: %s", exc)
                self._send_json({"error": f"Invalid video file: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not video_path.exists():
                self._send_json({"error": "Video file not found"}, status=HTTPStatus.NOT_FOUND)
                return

            logger.info("Starting video export of %s with %d words", video_file, len(edited_words))

            # Build segments to keep (non-deleted words)
            segments = build_segments(edited_words)

            if not segments:
                self._send_json({"error": "No segments to export - all words deleted?"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Generate output filename
            stem = video_path.stem
            output_filename = f"{stem}_edited.mp4"
            output_path = WORKSPACE_DIR / output_filename

            # Create job and run export in background thread
            job_id = job_manager.create_job("export", f"Export edited video: {video_file}")

            def run_export():
                success = export_video_ffmpeg(job_id, video_path, segments, output_path)
                if success:
                    refresh_workspace_cache()

            thread = threading.Thread(target=run_export, daemon=True)
            thread.start()

            self._send_json({
                "status": "started",
                "job_id": job_id,
                "output_file": output_filename
            })
            return

        if path == "/api/export-audio":
            if WORKSPACE_DIR is None:
                logger.warning("Audio export requested without workspace")
                self._send_json({"error": "workspace not set"}, status=HTTPStatus.BAD_REQUEST)
                return

            data = self._read_json_body()
            source_file = data.get("sourceFile")  # Can be video or audio
            edited_words = data.get("editedWords", [])

            if not source_file:
                self._send_json({"error": "sourceFile required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not edited_words:
                self._send_json({"error": "editedWords array required"}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                source_path = workspace_path_from_name(source_file)
            except Exception as exc:
                logger.error("Invalid source file path: %s", exc)
                self._send_json({"error": f"Invalid source file: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not source_path.exists():
                self._send_json({"error": "Source file not found"}, status=HTTPStatus.NOT_FOUND)
                return

            logger.info("Starting audio export from %s with %d words", source_file, len(edited_words))

            # Build segments to keep (non-deleted words)
            segments = build_segments(edited_words)

            if not segments:
                self._send_json({"error": "No segments to export - all words deleted?"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Generate output filename
            stem = source_path.stem
            output_filename = f"{stem}_edited.mp3"
            output_path = WORKSPACE_DIR / output_filename

            # Create job and run export in background thread
            job_id = job_manager.create_job("export", f"Export edited audio: {source_file}")

            def run_export():
                success = export_audio_ffmpeg(job_id, source_path, segments, output_path)
                if success:
                    refresh_workspace_cache()

            thread = threading.Thread(target=run_export, daemon=True)
            thread.start()

            self._send_json({
                "status": "started",
                "job_id": job_id,
                "output_file": output_filename
            })
            return

        if path == "/api/save-transcript-edits":
            if user_db is None:
                self._send_json({"error": "database not available"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return

            # Get user info from session
            username = session.get("user") if session else None
            if not username:
                self._send_json({"error": "not authenticated"}, status=HTTPStatus.UNAUTHORIZED)
                return

            user = user_db.get_user_by_username(username)
            if not user:
                self._send_json({"error": "user not found"}, status=HTTPStatus.UNAUTHORIZED)
                return

            data = self._read_json_body()
            project_name = data.get("projectName")
            transcript_file = data.get("transcriptFile")
            deleted_indices = data.get("deletedIndices", [])

            if not project_name:
                self._send_json({"error": "projectName required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not transcript_file:
                self._send_json({"error": "transcriptFile required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not isinstance(deleted_indices, list):
                self._send_json({"error": "deletedIndices must be an array"}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                # Store as JSON
                edits_json = json.dumps({"deletedIndices": deleted_indices})
                user_db.save_transcript_edits(
                    user_id=user['id'],
                    project_name=project_name,
                    transcript_file=transcript_file,
                    edits_json=edits_json
                )
                self._send_json({
                    "status": "saved",
                    "deletedCount": len(deleted_indices)
                })
            except Exception as e:
                logger.error("Failed to save transcript edits: %s", e)
                self._send_json({"error": f"Failed to save: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/load-transcript-edits":
            if user_db is None:
                # Return empty edits if database not available
                self._send_json({"deletedIndices": []})
                return

            # Get user info from session
            username = session.get("user") if session else None
            if not username:
                # Return empty edits if not authenticated
                self._send_json({"deletedIndices": []})
                return

            user = user_db.get_user_by_username(username)
            if not user:
                self._send_json({"deletedIndices": []})
                return

            params = parse_qs(parsed.query)
            project_name = params.get("projectName", [None])[0]
            transcript_file = params.get("transcriptFile", [None])[0]

            if not project_name or not transcript_file:
                self._send_json({"deletedIndices": []})
                return

            try:
                edits_json = user_db.load_transcript_edits(
                    user_id=user['id'],
                    project_name=project_name,
                    transcript_file=transcript_file
                )

                if edits_json:
                    edits_data = json.loads(edits_json)
                    self._send_json(edits_data)
                else:
                    self._send_json({"deletedIndices": []})
            except Exception as e:
                logger.error("Failed to load transcript edits: %s", e)
                self._send_json({"deletedIndices": []})
            return

        if path == "/api/contact":
            data = self._read_json_body()
            email = (data.get("email") or "").strip()
            message = (data.get("message") or "").strip()

            if not email or "@" not in email:
                self._send_json({"error": "Valid email address required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not message:
                self._send_json({"error": "Message is required"}, status=HTTPStatus.BAD_REQUEST)
                return

            # Save to comments.txt in data directory
            comments_file = DATA_DIR / "comments.txt"
            try:
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                comment_entry = f"\n{'='*80}\n"
                comment_entry += f"Date: {timestamp}\n"
                comment_entry += f"Email: {email}\n"
                comment_entry += f"Message:\n{message}\n"
                comment_entry += f"{'='*80}\n"

                # Append to comments file
                with open(comments_file, "a", encoding="utf-8") as f:
                    f.write(comment_entry)

                logger.info("Contact form submission from %s saved", email)
                self._send_json({"status": "success", "message": "Thank you for your message!"})
            except OSError as exc:
                logger.error("Failed to save contact form: %s", exc)
                self._send_json({"error": "Failed to save message. Please try again."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Podfree local server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.chdir(STATIC_DIR)
    server = ThreadingHTTPServer((args.host, args.port), PodfreeRequestHandler)
    print(f"Serving Podfree on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
