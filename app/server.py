#!/usr/bin/env python3
"""Podfree local development server.

This server hosts the web UI, exposes helper APIs, and runs automation scripts
against a user-selected workspace folder that contains the episode assets
(audio, video, markdown notes, etc.).
"""

from __future__ import annotations

import argparse
import cgi
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
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

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
TEMPLATES_DIR.mkdir(exist_ok=True)
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
        "files": {
            "notes": None,
            "srt": None,
            "video": None,
            "audio": None,
            "proxy": None,
            "transcript_json": None,
        },
        "options": {
            "notes": [],
            "srt": [],
            "video": [],
            "audio": [],
            "transcript_json": [],
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

    def is_proxy(name: str) -> bool:
        lowered = name.lower()
        return any(keyword in lowered for keyword in ("proxy", "light", "ultra", "low"))

    video_name = next((name for name in mp4_files if not is_proxy(name)), None)
    if video_name is None and mp4_files:
        video_name = mp4_files[0]

    proxy_name = next((name for name in mp4_files if is_proxy(name) and name != video_name), None)

    has_video = video_name is not None
    summary = {
        "path": str(ws),
        "valid": has_video,
        "files": {
            "notes": notes_name,
            "srt": srt_files[0] if srt_files else None,
            "video": video_name,
            "audio": mp3_files[0] if mp3_files else None,
            "proxy": proxy_name,
            "transcript_json": json_transcript_files[0] if json_transcript_files else None,
        },
        "options": {
            "notes": [p.name for p in md_candidates],
            "srt": srt_files,
            "video": mp4_files,
            "audio": mp3_files,
            "transcript_json": json_transcript_files,
        },
        "timestamp": time.time(),
        "missing": {
            "video": not has_video,
            "srt": not srt_files,
            "notes": not md_candidates,
            "audio": not mp3_files,
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


def list_projects() -> Dict[str, Any]:
    projects = []
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
    "generate_covers.py": "Generate Covers",
    "create_ghost_post.py": "Post to Ghost CMS",
    "castopod_post.py": "Castopod Draft",
    "post_to_linkedin.py": "Post to LinkedIn",
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
    scripts: list[Dict[str, Any]] = []
    for script in sorted(SCRIPTS_DIR.glob("*.py")):
        name = script.name
        label = _script_label(name)
        status = "unknown"
        outputs: list[str] = []

        if summary:
            if name == "deepgram_transcribe_debates.py":
                outputs = list(options.get("transcript_json", []))
                transcript_file = files.get("transcript_json")
                if transcript_file and transcript_file not in outputs:
                    outputs.insert(0, transcript_file)
                status = "ready" if outputs else "missing"
            elif name == "extract_audio_from_video.py":
                outputs = list(options.get("audio", []))
                audio_file = files.get("audio")
                if audio_file and audio_file not in outputs:
                    outputs.insert(0, audio_file)
                status = "ready" if outputs else "missing"
            elif name == "generate_covers.py":
                cover_files: list[str] = []
                workspace_path = summary.get("path")
                if workspace_path:
                    ws = Path(workspace_path)
                    for cover_name in ("youtube-cover.jpg", "podcast-cover.jpg"):
                        if (ws / cover_name).is_file():
                            cover_files.append(cover_name)
                outputs = cover_files
                status = "ready" if cover_files else "missing"
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
                "label": label,
                "status": status,
                "outputs": outputs,
            }
        )
    return scripts


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
                out_time_ms = float(line.split("=", 1)[1])
                progress = (out_time_ms / 1_000_000.0) / duration * 100.0
                job_manager.update_job(job_id, progress=progress)
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


class PodfreeRequestHandler(SimpleHTTPRequestHandler):
    server_version = "Podfree/1.0"
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
        if path in {"/login", "/login/", "/login.html"}:
            return True
        if path in {"/favicon.ico", "/favicon.png", "/logo.svg"}:
            return True
        if path.startswith("/api/login"):
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
            self._send_json(list_projects())
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
            if username == AUTH_USERNAME and password == AUTH_PASSWORD:
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

            summary = summarize_directory(project_dir)
            summary["name"] = project_dir.name
            self._send_json({"status": "uploaded", "saved": saved, "project": summary})
            return

        if path == "/api/projects/delete":
            data = self._read_json_body()
            name = data.get("name")
            if not name:
                self._send_json({"error": "project name required"}, status=HTTPStatus.BAD_REQUEST)
                return
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
