#!/usr/bin/env python3
"""Database management for Podfree Editor multi-user system."""

import sqlite3
import bcrypt
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger("podfree.database")


class UserDB:
    """Manages user authentication and credit tracking with SQLite."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self):
        """Initialize database schema."""
        conn = self._get_connection()

        try:
            # Users table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                )
            """)

            # Sessions table (for future use)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)

            # Credits table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS credits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    credits_hours REAL DEFAULT 0,
                    week_year TEXT NOT NULL,
                    allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(user_id, week_year)
                )
            """)

            # Usage table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    duration_hours REAL NOT NULL,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    month_year TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_credits_user_week ON credits(user_id, week_year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_month ON usage(user_id, month_year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_uploaded ON usage(uploaded_at)")

            conn.commit()
            logger.info("Database initialized at %s", self.db_path)

        except Exception as e:
            logger.error("Failed to initialize database: %s", e)
            raise
        finally:
            conn.close()

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        is_admin: bool = False,
        initial_credits: float = 2.0
    ) -> int:
        """
        Create a new user with hashed password.
        Returns user_id on success, raises exception on failure.
        """
        # Hash password
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, email, password_hash, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username.strip(), email.strip().lower(), password_hash, is_admin, datetime.utcnow())
            )
            user_id = cursor.lastrowid

            # Allocate initial credits
            week_year = datetime.utcnow().strftime("%Y-W%W")
            conn.execute(
                """
                INSERT INTO credits (user_id, credits_hours, week_year, allocated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, initial_credits, week_year, datetime.utcnow())
            )

            conn.commit()
            logger.info("Created user: %s (ID: %d) with %s hours initial credits", username, user_id, initial_credits)
            return user_id

        except sqlite3.IntegrityError as e:
            if "username" in str(e).lower():
                raise ValueError("Username already exists")
            elif "email" in str(e).lower():
                raise ValueError("Email already exists")
            else:
                raise ValueError(f"User creation failed: {e}")
        finally:
            conn.close()

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """
        Authenticate user with username and password.
        Returns user dict if valid, None if invalid.
        """
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id, username, email, password_hash, is_admin, created_at FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),)
            ).fetchone()

            if not row:
                return None

            # Verify password
            if bcrypt.checkpw(password.encode('utf-8'), row['password_hash']):
                # Update last login
                conn.execute(
                    "UPDATE users SET last_login = ? WHERE id = ?",
                    (datetime.utcnow(), row['id'])
                )
                conn.commit()

                return {
                    'id': row['id'],
                    'username': row['username'],
                    'email': row['email'],
                    'is_admin': bool(row['is_admin']),
                    'created_at': row['created_at']
                }

            return None

        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username (case-insensitive)."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id, username, email, is_admin, created_at, last_login FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),)
            ).fetchone()

            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by ID."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id, username, email, is_admin, created_at, last_login FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()

            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def get_user_credits(self, user_id: int) -> float:
        """
        Get total available credits for user.
        Returns sum of all allocated credits minus usage.
        """
        conn = self._get_connection()
        try:
            # Get total allocated credits
            row = conn.execute(
                "SELECT COALESCE(SUM(credits_hours), 0) as total FROM credits WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            total_allocated = row['total']

            # Get total used credits
            row = conn.execute(
                "SELECT COALESCE(SUM(duration_hours), 0) as total FROM usage WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            total_used = row['total']

            return round(total_allocated - total_used, 2)

        finally:
            conn.close()

    def allocate_credits(self, user_id: int, credits_hours: float, week_year: Optional[str] = None):
        """
        Allocate credits to user for a specific week.
        If week_year is None, uses current week.
        """
        if week_year is None:
            week_year = datetime.utcnow().strftime("%Y-W%W")

        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO credits (user_id, credits_hours, week_year, allocated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, week_year) DO UPDATE SET credits_hours = credits_hours + ?
                """,
                (user_id, credits_hours, week_year, datetime.utcnow(), credits_hours)
            )
            conn.commit()
            logger.info("Allocated %.2f hours to user_id=%d for week %s", credits_hours, user_id, week_year)
        finally:
            conn.close()

    def ensure_weekly_credits(self, user_id: int, weekly_amount: float = 2.0):
        """
        Ensure user has credits for current week.
        If not, allocate them. This is called on-demand.
        """
        week_year = datetime.utcnow().strftime("%Y-W%W")

        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM credits WHERE user_id = ? AND week_year = ?",
                (user_id, week_year)
            ).fetchone()

            if not row:
                # No credits for this week yet, allocate them
                self.allocate_credits(user_id, weekly_amount, week_year)

        finally:
            conn.close()

    def log_usage(
        self,
        user_id: int,
        project_name: str,
        file_name: str,
        file_path: str,
        duration_seconds: float
    ):
        """Log media file upload usage."""
        duration_hours = round(duration_seconds / 3600, 2)
        month_year = datetime.utcnow().strftime("%Y-%m")

        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO usage (user_id, project_name, file_name, file_path,
                                   duration_seconds, duration_hours, uploaded_at, month_year)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, project_name, file_name, file_path, duration_seconds,
                 duration_hours, datetime.utcnow(), month_year)
            )
            conn.commit()
            logger.info("Logged usage: user_id=%d, file=%s, duration=%.2fh", user_id, file_name, duration_hours)
        finally:
            conn.close()

    def get_monthly_usage(self, user_id: int) -> List[Dict[str, Any]]:
        """Get usage statistics grouped by month."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    month_year,
                    COUNT(*) as files_count,
                    SUM(duration_hours) as hours_used
                FROM usage
                WHERE user_id = ?
                GROUP BY month_year
                ORDER BY month_year DESC
                LIMIT 12
                """,
                (user_id,)
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            conn.close()

    def get_recent_uploads(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent file uploads for user."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT file_name, duration_hours, uploaded_at, project_name
                FROM usage
                WHERE user_id = ?
                ORDER BY uploaded_at DESC
                LIMIT ?
                """,
                (user_id, limit)
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            conn.close()

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get all users (admin function)."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT id, username, email, is_admin, created_at, last_login FROM users ORDER BY created_at DESC"
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            conn.close()
