"""Authentication — Cookie-based sessions with SQLite storage."""
import hashlib
import hmac
import os
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from database import _get_conn

# Session lifetime: 30 days
SESSION_TTL = 30 * 24 * 3600
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def _hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _password_hasher.hash(password)


def _verify_password(password: str, stored: str) -> bool:
    """Verify Argon2id hashes and support one-time migration from legacy SHA-256."""
    if stored.startswith("$argon2"):
        try:
            return _password_hasher.verify(stored, password)
        except (InvalidHashError, VerifyMismatchError):
            return False

    try:
        salt, expected = stored.split(":", 1)
    except ValueError:
        return False
    actual = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hmac.compare_digest(actual, expected)


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
    """Create the initial user only when explicit credentials are configured."""
    username = username or os.getenv("SMART_PDF_ADMIN_USER", "").strip()
    password = password or os.getenv("SMART_PDF_ADMIN_PASSWORD", "")
    if not username or not password:
        return False
    if len(password) < 12:
        raise RuntimeError("SMART_PDF_ADMIN_PASSWORD must contain at least 12 characters")
    conn = _get_conn()
    row = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, _hash_password(password), time.time()),
        )
        conn.commit()
    conn.close()
    return True


def authenticate(username: str, password: str) -> str | None:
    """Verify credentials and return session token, or None."""
    conn = _get_conn()
    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        conn.close()
        return None

    if not row["password_hash"].startswith("$argon2"):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (_hash_password(password), username),
        )

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
