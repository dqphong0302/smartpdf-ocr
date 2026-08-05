"""SQLite database for persisting OCR job history."""
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "ocr_history.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'uploaded',
            total_pages INTEGER DEFAULT 0,
            selected_pages TEXT DEFAULT '[]',
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS page_results (
            job_id TEXT NOT NULL,
            page_num INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            classification TEXT DEFAULT '',
            method TEXT,
            text TEXT DEFAULT '',
            html_text TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            time_taken REAL DEFAULT 0.0,
            error TEXT,
            PRIMARY KEY (job_id, page_num),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()


# ── Job CRUD ─────────────────────────────────────────────────

def save_job(job) -> None:
    """Save or update a job (from dataclass) to the database."""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO jobs
            (job_id, filename, filepath, status, total_pages, selected_pages,
             created_at, started_at, completed_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job.job_id, job.filename, job.filepath, job.status.value,
        job.total_pages, json.dumps(job.selected_pages),
        job.created_at, job.started_at, job.completed_at, job.error,
    ))
    conn.commit()
    conn.close()


def save_page_result(job_id: str, page) -> None:
    """Save or update a single page result."""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO page_results
            (job_id, page_num, status, classification, method, text, html_text,
             confidence, time_taken, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id, page.page_num, page.status.value, page.classification,
        page.method, page.text, page.html_text,
        page.confidence, page.time_taken, page.error,
    ))
    conn.commit()
    conn.close()


def load_job_dict(job_id: str, include_text: bool = False) -> dict | None:
    """Load a job from DB as a dict (used for API responses)."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return None

    pages_data = {}
    page_rows = conn.execute(
        "SELECT * FROM page_results WHERE job_id = ? ORDER BY page_num", (job_id,)
    ).fetchall()
    conn.close()

    methods = {"digital": 0, "tesseract": 0, "vision": 0, "skipped": 0}
    completed = 0
    total_conf = 0.0
    conf_count = 0

    for p in page_rows:
        d = {
            "page_num": p["page_num"],
            "status": p["status"],
            "classification": p["classification"],
            "method": p["method"],
            "text": p["text"] if include_text else None,
            "html_text": p["html_text"] if include_text else None,
            "confidence": p["confidence"],
            "time_taken": round(p["time_taken"], 2),
            "error": p["error"],
        }
        if not include_text:
            d.pop("text", None)
        pages_data[str(p["page_num"])] = d

        if p["status"] == "completed":
            if p["method"]:
                methods[p["method"]] = methods.get(p["method"], 0) + 1
            # Only count non-skipped pages as completed for progress
            if p["method"] and p["method"] != "skipped":
                completed += 1
                if p["confidence"] > 0:
                    total_conf += p["confidence"]
                    conf_count += 1

    selected = json.loads(row["selected_pages"]) if row["selected_pages"] else []
    elapsed = 0
    if row["started_at"]:
        end = row["completed_at"] or time.time()
        elapsed = round(end - row["started_at"], 2)

    return {
        "job_id": row["job_id"],
        "filename": row["filename"],
        "status": row["status"],
        "total_pages": row["total_pages"],
        "selected_pages": selected,
        "pages": pages_data,
        "elapsed_time": elapsed,
        "created_at": row["created_at"],
        "error": row["error"],
        "summary": {
            "completed": completed,
            "total": len(selected) if selected else row["total_pages"],
            "methods": methods,
            "avg_confidence": round(total_conf / conf_count, 1) if conf_count else 0,
        },
    }


def list_jobs_from_db() -> list[dict]:
    """List all jobs as summary dicts, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC"
    ).fetchall()

    results = []
    for row in rows:
        # Get quick summary from page_results
        stats = conn.execute("""
            SELECT
                COUNT(CASE WHEN status='completed' AND (method IS NULL OR method != 'skipped') THEN 1 END) as completed,
                SUM(CASE WHEN method='digital' AND status='completed' THEN 1 ELSE 0 END) as m_digital,
                SUM(CASE WHEN method='tesseract' AND status='completed' THEN 1 ELSE 0 END) as m_tesseract,
                SUM(CASE WHEN method='vision' AND status='completed' THEN 1 ELSE 0 END) as m_vision,
                SUM(CASE WHEN method='skipped' AND status='completed' THEN 1 ELSE 0 END) as m_skipped,
                AVG(CASE WHEN confidence > 0 AND method != 'skipped' THEN confidence END) as avg_conf
            FROM page_results WHERE job_id = ?
        """, (row["job_id"],)).fetchone()

        selected = json.loads(row["selected_pages"]) if row["selected_pages"] else []
        elapsed = round((row["completed_at"] or 0) - (row["started_at"] or 0), 2) if row["started_at"] else 0

        results.append({
            "job_id": row["job_id"],
            "filename": row["filename"],
            "status": row["status"],
            "total_pages": row["total_pages"],
            "created_at": row["created_at"],
            "elapsed_time": elapsed,
            "summary": {
                "completed": stats["completed"] or 0,
                "total": len(selected) if selected else row["total_pages"],
                "methods": {
                    "digital": stats["m_digital"] or 0,
                    "tesseract": stats["m_tesseract"] or 0,
                    "vision": stats["m_vision"] or 0,
                    "skipped": stats["m_skipped"] or 0,
                },
                "avg_confidence": round(stats["avg_conf"], 1) if stats["avg_conf"] else 0,
            },
        })

    conn.close()
    return results


def delete_job_from_db(job_id: str) -> bool:
    """Delete a job and its page results."""
    conn = _get_conn()
    row = conn.execute("SELECT filepath FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return False

    conn.execute("DELETE FROM page_results WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

    # Clean up file
    filepath = row["filepath"]
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass
    return True


def cleanup_expired_jobs(max_age_days: int = 7) -> dict:
    """Delete jobs older than max_age_days. Returns cleanup stats."""
    cutoff = time.time() - (max_age_days * 86400)
    conn = _get_conn()
    rows = conn.execute(
        "SELECT job_id, filepath FROM jobs WHERE created_at < ?", (cutoff,)
    ).fetchall()

    deleted = 0
    files_removed = 0
    bytes_freed = 0

    for row in rows:
        job_id = row["job_id"]
        filepath = row["filepath"]

        # Delete from DB
        conn.execute("DELETE FROM page_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

        # Delete PDF file
        if filepath and os.path.exists(filepath):
            try:
                fsize = os.path.getsize(filepath)
                os.remove(filepath)
                files_removed += 1
                bytes_freed += fsize
            except OSError:
                pass
        deleted += 1

    conn.commit()
    conn.close()

    return {
        "deleted_jobs": deleted,
        "files_removed": files_removed,
        "bytes_freed": bytes_freed,
        "bytes_freed_mb": round(bytes_freed / (1024 * 1024), 1),
    }
