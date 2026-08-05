"""Authentication — Cookie-based sessions with SQLite storage."""
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

from database import _get_conn

# Session lifetime: 30 days
SESSION_TTL = 30 * 24 * 3600


def _hash_password(password: str, salt: str = "") -> str:
    """SHA-256 hash with salt."""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{hashed}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify password against stored hash."""
    salt = stored.split(":")[0]
    return _hash_password(password, salt) == stored


def init_auth_tables():
    """Create auth tables."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()


def seed_default_user(username: str | None = None, password: str | None = None):
    """Create default user if not exists, using env-driven credentials."""
    username = username or os.getenv("SMART_PDF_ADMIN_USER", "admin")
    password = password or os.getenv("SMART_PDF_ADMIN_PASSWORD", "changeme")
    conn = _get_conn()
    row = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, _hash_password(password), time.time()),
        )
        conn.commit()
    conn.close()


def authenticate(username: str, password: str) -> str | None:
    """Verify credentials and return session token, or None."""
    conn = _get_conn()
    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        conn.close()
        return None

    # Create session
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, username, now, now + SESSION_TTL),
    )
    # Clean up expired sessions
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.commit()
    conn.close()
    return token


def validate_session(token: str) -> str | None:
    """Return username if session is valid, else None."""
    if not token:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT username FROM sessions WHERE token = ? AND expires_at > ?",
        (token, time.time()),
    ).fetchone()
    conn.close()
    return row["username"] if row else None


def logout(token: str) -> None:
    """Delete session."""
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()
