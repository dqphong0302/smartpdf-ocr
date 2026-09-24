"""SQLite database for persisting OCR job history."""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.getenv("DATABASE_PATH", str(Path(__file__).parent / "data" / "ocr_history.db")))


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

        CREATE TABLE IF NOT EXISTS client_quotas (
            client_id TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            request_count INTEGER DEFAULT 0,
            total_pages INTEGER DEFAULT 0,
            last_request_at REAL NOT NULL,
            PRIMARY KEY (client_id, usage_date)
        );

        CREATE TABLE IF NOT EXISTS api_jobs (
            job_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'processing',
            total_pages INTEGER NOT NULL DEFAULT 0,
            completed_pages INTEGER NOT NULL DEFAULT 0,
            selected_pages TEXT NOT NULL DEFAULT '[]',
            method TEXT NOT NULL DEFAULT 'auto',
            pages_json TEXT NOT NULL DEFAULT '{}',
            html_result TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            expires_at REAL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS api_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'processing',
            total_files INTEGER NOT NULL DEFAULT 0,
            completed_files INTEGER NOT NULL DEFAULT 0,
            results_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            completed_at REAL,
            expires_at REAL,
            error TEXT
        );

        PRAGMA user_version = 2;
    """)
    conn.commit()
    conn.close()


def save_api_job(job_id: str, job: dict) -> None:
    """Persist an API OCR job so status and results survive process restarts."""
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO api_jobs (
            job_id, filename, filepath, status, total_pages, completed_pages,
            selected_pages, method, pages_json, html_result, created_at,
            started_at, completed_at, expires_at, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            filename=excluded.filename,
            filepath=excluded.filepath,
            status=excluded.status,
            total_pages=excluded.total_pages,
            completed_pages=excluded.completed_pages,
            selected_pages=excluded.selected_pages,
            method=excluded.method,
            pages_json=excluded.pages_json,
            html_result=excluded.html_result,
            started_at=excluded.started_at,
            completed_at=excluded.completed_at,
            expires_at=excluded.expires_at,
            error=excluded.error
        """,
        (
            job_id,
            job.get("filename", ""),
            job.get("filepath", ""),
            job.get("status", "processing"),
            int(job.get("total_pages", 0)),
            int(job.get("completed_pages", 0)),
            json.dumps(job.get("selected_pages", [])),
            job.get("method", "auto"),
            json.dumps(job.get("pages", {}), ensure_ascii=False),
            job.get("html_result"),
            float(job.get("created_at", time.time())),
            job.get("started_at"),
            job.get("completed_at"),
            job.get("expires_at"),
            job.get("error"),
        ),
    )
    conn.commit()
    conn.close()


def load_api_job(job_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM api_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["selected_pages"] = json.loads(result.pop("selected_pages") or "[]")
    result["pages"] = json.loads(result.pop("pages_json") or "{}")
    return result


def save_api_batch(batch_id: str, batch: dict) -> None:
    """Persist API batch metadata and child results."""
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO api_batches (
            batch_id, status, total_files, completed_files, results_json,
            created_at, completed_at, expires_at, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
            status=excluded.status,
            total_files=excluded.total_files,
            completed_files=excluded.completed_files,
            results_json=excluded.results_json,
            completed_at=excluded.completed_at,
            expires_at=excluded.expires_at,
            error=excluded.error
        """,
        (
            batch_id,
            batch.get("status", "processing"),
            int(batch.get("total_files", 0)),
            int(batch.get("completed_files", 0)),
            json.dumps(batch.get("results", []), ensure_ascii=False),
            float(batch.get("created_at", time.time())),
            batch.get("completed_at"),
            batch.get("expires_at"),
            batch.get("error"),
        ),
    )
    conn.commit()
    conn.close()


def load_api_batch(batch_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM api_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["results"] = json.loads(result.pop("results_json") or "[]")
    return result


def reconcile_interrupted_jobs(reason: str) -> dict[str, int]:
    """Move work orphaned by a process restart to a stable terminal state."""
    now = time.time()
    conn = _get_conn()
    ui_ids = [
        row["job_id"]
        for row in conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('analyzing', 'processing')"
        ).fetchall()
    ]
    api_count = conn.execute(
        """
        UPDATE api_jobs
        SET status='interrupted', completed_at=?, expires_at=COALESCE(expires_at, ?),
            error=COALESCE(error, ?)
        WHERE status='processing'
        """,
        (now, now + 3600, reason),
    ).rowcount
    batch_count = conn.execute(
        """
        UPDATE api_batches
        SET status='interrupted', completed_at=?, expires_at=COALESCE(expires_at, ?),
            error=COALESCE(error, ?)
        WHERE status='processing'
        """,
        (now, now + 3600, reason),
    ).rowcount
    if ui_ids:
        placeholders = ",".join("?" for _ in ui_ids)
        conn.execute(
            f"UPDATE jobs SET status='interrupted', completed_at=?, error=COALESCE(error, ?) "
            f"WHERE job_id IN ({placeholders})",
            (now, reason, *ui_ids),
        )
        conn.execute(
            f"UPDATE page_results SET status='failed', error=COALESCE(error, ?) "
            f"WHERE status IN ('analyzing', 'processing') AND job_id IN ({placeholders})",
            (reason, *ui_ids),
        )
    conn.commit()
    conn.close()
    return {"ui": len(ui_ids), "api": api_count, "batch": batch_count}


def cleanup_expired_api_records(now: float | None = None) -> dict[str, int]:
    """Delete expired persisted API results and return removal counts."""
    cutoff = now if now is not None else time.time()
    conn = _get_conn()
    api_count = conn.execute(
        "DELETE FROM api_jobs WHERE expires_at IS NOT NULL AND expires_at <= ?", (cutoff,)
    ).rowcount
    batch_count = conn.execute(
        "DELETE FROM api_batches WHERE expires_at IS NOT NULL AND expires_at <= ?", (cutoff,)
    ).rowcount
    conn.commit()
    conn.close()
    return {"api": api_count, "batch": batch_count}


# ── Client / IP Quota Rate Limiting ──────────────────────────

def check_and_increment_client_quota(client_id: str, page_count: int, max_pages: int = 5, max_daily: int = 5) -> dict:
    """
    Check if a client IP has quota remaining for today (Vietnam UTC+7 timezone).
    Returns usage stats dict or raises ValueError.
    """
    if page_count > max_pages:
        raise ValueError(f"Bản miễn phí chỉ cho phép tối đa {max_pages} trang mỗi lần OCR (bạn đang gửi {page_count} trang).")

    from datetime import datetime, timedelta, timezone
    vn_now = datetime.now(timezone(timedelta(hours=7)))
    today_str = vn_now.strftime("%Y-%m-%d")

    conn = _get_conn()
    row = conn.execute(
        "SELECT request_count, total_pages FROM client_quotas WHERE client_id = ? AND usage_date = ?",
        (client_id, today_str)
    ).fetchone()

    current_count = row["request_count"] if row else 0

    if current_count >= max_daily:
        conn.close()
        midnight = vn_now.replace(hour=23, minute=59, second=59)
        rem_secs = max(0, int((midnight - vn_now).total_seconds()))
        rem_hrs = rem_secs // 3600
        rem_mins = (rem_secs % 3600) // 60
        raise PermissionError(f"Thiết bị của bạn đã dùng hết {max_daily}/{max_daily} lượt OCR miễn phí hôm nay. Lượt mới sẽ tự động làm mới vào 00:00 (còn khoảng {rem_hrs} giờ {rem_mins} phút).")

    new_count = current_count + 1
    new_pages = (row["total_pages"] if row else 0) + page_count

    conn.execute("""
        INSERT OR REPLACE INTO client_quotas (client_id, usage_date, request_count, total_pages, last_request_at)
        VALUES (?, ?, ?, ?, ?)
    """, (client_id, today_str, new_count, new_pages, time.time()))
    conn.commit()
    conn.close()

    return {
        "client_id": client_id,
        "date": today_str,
        "used": new_count,
        "remaining": max_daily - new_count,
        "max_daily": max_daily,
    }


def get_client_quota(client_id: str, max_daily: int = 5) -> dict:
    """Get current day quota usage for a client."""
    from datetime import datetime, timedelta, timezone
    vn_now = datetime.now(timezone(timedelta(hours=7)))
    today_str = vn_now.strftime("%Y-%m-%d")

    conn = _get_conn()
    row = conn.execute(
        "SELECT request_count, total_pages FROM client_quotas WHERE client_id = ? AND usage_date = ?",
        (client_id, today_str)
    ).fetchone()
    conn.close()

    used = row["request_count"] if row else 0
    return {
        "client_id": client_id,
        "date": today_str,
        "used": used,
        "remaining": max(0, max_daily - used),
        "max_daily": max_daily,
    }


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
        elapsed = round((row["completed_at"] or time.time()) - row["started_at"], 2) if row["started_at"] else 0

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

    # Clean up file and extracted images
    filepath = row["filepath"]
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass

    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploads"))
    img_dir = upload_dir / "extracted_images" / job_id
    if img_dir.exists():
        shutil.rmtree(img_dir, ignore_errors=True)

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
    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploads"))

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

        # Delete extracted images directory
        img_dir = upload_dir / "extracted_images" / job_id
        if img_dir.exists():
            try:
                shutil.rmtree(img_dir, ignore_errors=True)
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
